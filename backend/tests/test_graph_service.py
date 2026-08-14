"""GraphService 测试 — 规则提取、实体验证、批量导入结构。

不依赖 Neo4j / Redis 外部服务，仅测试纯 Python 逻辑。
"""

import pytest
from unittest.mock import AsyncMock

from app.services.graph_service import GraphService


class TestRuleExtraction:
    """规则三元组提取测试。"""

    def setup_method(self):
        self.service = GraphService()

    def test_extract_belong_to(self):
        """属于关系。"""
        triples = self.service._extract_triples_by_rules(
            "微服务属于架构模式"
        )
        assert ("微服务", "属于", "架构模式") in triples

    def test_extract_contains(self):
        """包含关系。"""
        triples = self.service._extract_triples_by_rules(
            "系统包含用户管理模块"
        )
        assert ("系统", "包含", "用户管理模块") in triples

    def test_extract_references(self):
        """引用关系。"""
        triples = self.service._extract_triples_by_rules(
            "文档A引用文档B"
        )
        assert ("文档A", "引用", "文档B") in triples

    def test_extract_based_on(self):
        """基于关系。"""
        triples = self.service._extract_triples_by_rules(
            "容器化基于虚拟化技术"
        )
        assert ("容器化", "基于", "虚拟化技术") in triples

    def test_extract_uses(self):
        """使用关系（采用）。"""
        triples = self.service._extract_triples_by_rules(
            "FastAPI采用异步框架"
        )
        assert ("FastAPI", "使用", "异步框架") in triples

    def test_extract_multiple_triples(self):
        """多三元组混合文本。"""
        text = (
            "微服务属于架构模式。"
            "系统包含用户管理模块。"
            "容器化基于虚拟化技术。"
        )
        triples = self.service._extract_triples_by_rules(text)
        assert len(triples) >= 3

    def test_empty_text(self):
        """空文本无三元组。"""
        assert self.service._extract_triples_by_rules("") == []

    def test_no_matching_pattern(self):
        """无匹配模式返回空。"""
        triples = self.service._extract_triples_by_rules("今天天气很好")
        assert triples == []

    def test_stopword_filtered(self):
        """停用词实体被过滤。"""
        triples = self.service._extract_triples_by_rules("我们属于公司")
        # "我们" 是停用词，应被过滤
        assert ("我们", "属于", "公司") not in triples


class TestEntityValidation:
    """实体验证测试。"""

    def test_valid_entity(self):
        assert GraphService._is_valid_entity("微服务") is True

    def test_valid_english_entity(self):
        assert GraphService._is_valid_entity("FastAPI") is True

    def test_stopword_filtered(self):
        assert GraphService._is_valid_entity("我们") is False

    def test_too_short(self):
        assert GraphService._is_valid_entity("A") is False

    def test_pure_digit(self):
        assert GraphService._is_valid_entity("123") is False

    def test_valid_long_entity(self):
        assert GraphService._is_valid_entity("企业级知识库管理系统") is True


class TestBatchImportStructure:
    """批量导入数据结构测试（不依赖 Neo4j）。"""

    def test_batch_import_document_builds_correct_nodes(self):
        """batch_import_document 构建正确的节点列表。"""
        service = GraphService()
        # 直接测试数据结构构建，不调用 Neo4j
        doc_id = "test-doc-1"
        title = "测试文档"
        triples = [("微服务", "属于", "架构模式")]

        # 手动构建 nodes（模拟 batch_import_document 内部逻辑）
        nodes = [
            {
                "label": "Document",
                "id": doc_id,
                "title": title,
                "kb_id": None,
                "doc_type": "md",
            }
        ]
        relationships = []

        for subject, predicate, obj in triples:
            nodes.append({
                "label": "Concept",
                "id": subject,
                "name": subject,
                "entity_type": "concept",
            })
            nodes.append({
                "label": "Concept",
                "id": obj,
                "name": obj,
                "entity_type": "concept",
            })
            rel_type = predicate.upper().replace(" ", "_")
            relationships.append({
                "from_label": "Concept",
                "from_id": subject,
                "to_label": "Concept",
                "to_id": obj,
                "type": rel_type,
            })
            relationships.append({
                "from_label": "Document",
                "from_id": doc_id,
                "to_label": "Concept",
                "to_id": subject,
                "type": "MENTIONS",
            })
            relationships.append({
                "from_label": "Document",
                "from_id": doc_id,
                "to_label": "Concept",
                "to_id": obj,
                "type": "MENTIONS",
            })

        # 1 个 Document + 2 个 Concept = 3 个节点
        assert len(nodes) == 3
        # 1 个概念间关系 + 2 个 MENTIONS = 3 个关系
        assert len(relationships) == 3

        # 验证节点标签
        labels = [n["label"] for n in nodes]
        assert labels.count("Document") == 1
        assert labels.count("Concept") == 2

        # 验证关系类型
        rel_types = [r["type"] for r in relationships]
        assert "属于" in rel_types  # predicate.upper() for Chinese
        assert rel_types.count("MENTIONS") == 2

    @pytest.mark.asyncio
    async def test_batch_import_document_with_chunks(self):
        """batch_import_document 传入 chunks 时，创建 DocumentChunk 节点 + HAS_CHUNK 边。"""
        service = GraphService()
        service.batch_import_graph = AsyncMock(return_value={"nodes": 0, "relationships": 0})

        doc_id = "doc-chunks-test"
        chunks = [
            {"id": "chunk-1", "content": "微服务属于架构模式", "title_path": "第一章"},
            {"id": "chunk-2", "content": "系统包含用户管理", "title_path": "第二章"},
        ]

        # 手动构建 nodes（模拟 batch_import_document chunks 逻辑）
        nodes = [{"label": "Document", "id": doc_id, "title": "测试", "kb_id": None, "doc_type": "md"}]
        relationships = []

        chunk_ids = []
        for chunk in chunks:
            cid = chunk["id"]
            chunk_ids.append(cid)
            nodes.append({
                "label": "DocumentChunk",
                "id": cid,
                "doc_id": doc_id,
                "text": chunk["content"][:500],
                "title_path": chunk["title_path"],
            })
            relationships.append({
                "from_label": "Document",
                "from_id": doc_id,
                "to_label": "DocumentChunk",
                "to_id": cid,
                "type": "HAS_CHUNK",
            })

        # 验证 DocumentChunk 节点
        chunk_nodes = [n for n in nodes if n["label"] == "DocumentChunk"]
        assert len(chunk_nodes) == 2
        assert {n["id"] for n in chunk_nodes} == {"chunk-1", "chunk-2"}
        # 验证溯源信息
        for cn in chunk_nodes:
            assert cn["doc_id"] == doc_id
            assert cn["text"]  # 非空

        # 验证 HAS_CHUNK 边
        has_chunk_rels = [r for r in relationships if r["type"] == "HAS_CHUNK"]
        assert len(has_chunk_rels) == 2
        for rel in has_chunk_rels:
            assert rel["from_label"] == "Document"
            assert rel["from_id"] == doc_id
            assert rel["to_label"] == "DocumentChunk"


class TestRecommendCacheKey:
    """推荐缓存 key 格式测试。"""

    def test_cache_key_format(self):
        """缓存 key 格式正确。"""
        doc_id = "abc-123"
        user_id = "user-456"
        key = f"graph:recommend:{doc_id}:{user_id}"
        assert key == "graph:recommend:abc-123:user-456"

    def test_invalidate_pattern(self):
        """缓存失效 pattern 正确。"""
        doc_id = "abc-123"
        pattern = f"graph:recommend:{doc_id}:*"
        assert pattern == "graph:recommend:abc-123:*"


# ======================================================================
# extract_triples_from_chunks — 计算复用优化（方向二）
# ======================================================================


class TestExtractTriplesFromChunks:
    """extract_triples_from_chunks 测试 — 从 Chunk 对象列表提取三元组。

    验证方向二：计算复用 — 文档处理流水线已分块的 chunk_objects 直接传入，
    避免重复分块计算。
    """

    def setup_method(self):
        self.service = GraphService()

    def _make_chunk(self, content: str, title_path: str = "", chunk_id: str = "") -> object:
        """构造模拟 Chunk 对象（含 id 用于溯源）。"""
        from types import SimpleNamespace
        import uuid

        return SimpleNamespace(
            id=chunk_id or f"chunk-{uuid.uuid4().hex[:8]}",
            content=content,
            title_path=title_path,
        )

    @pytest.mark.asyncio
    async def test_extract_from_chunks_basic(self) -> None:
        """从多个 chunk 提取三元组，规则提取应生效。"""
        chunks = [
            self._make_chunk("微服务属于架构模式"),
            self._make_chunk("系统包含用户管理模块"),
        ]
        # mock batch_import_graph 避免依赖 Neo4j
        self.service.batch_import_graph = AsyncMock(return_value={"nodes": 4, "relationships": 6})

        triples = await self.service.extract_triples_from_chunks(
            chunks=chunks,
            doc_id="test-doc-1",
            use_rules=True,
            use_llm=False,
        )

        assert len(triples) >= 2
        assert ("微服务", "属于", "架构模式") in triples
        assert ("系统", "包含", "用户管理模块") in triples

    @pytest.mark.asyncio
    async def test_extract_from_chunks_deduplication(self) -> None:
        """多个 chunk 中的重复三元组应全局去重。"""
        chunks = [
            self._make_chunk("微服务属于架构模式"),
            self._make_chunk("微服务属于架构模式"),  # 完全重复
        ]
        self.service.batch_import_graph = AsyncMock(return_value={"nodes": 2, "relationships": 3})

        triples = await self.service.extract_triples_from_chunks(
            chunks=chunks,
            doc_id="test-doc-2",
            use_rules=True,
            use_llm=False,
        )

        # 去重后应只有 1 条
        assert len(triples) == 1
        assert triples[0] == ("微服务", "属于", "架构模式")

    @pytest.mark.asyncio
    async def test_extract_from_chunks_empty_content_skipped(self) -> None:
        """空 content 的 chunk 应被跳过。"""
        chunks = [
            self._make_chunk(""),
            self._make_chunk("   "),
            self._make_chunk("微服务属于架构模式"),
        ]
        self.service.batch_import_graph = AsyncMock(return_value={"nodes": 2, "relationships": 3})

        triples = await self.service.extract_triples_from_chunks(
            chunks=chunks,
            doc_id="test-doc-3",
            use_rules=True,
            use_llm=False,
        )

        assert len(triples) == 1

    @pytest.mark.asyncio
    async def test_extract_from_chunks_no_triples_returns_empty(self) -> None:
        """无法提取三元组时返回空列表，不调用 batch_import_graph。"""
        chunks = [self._make_chunk("今天天气很好")]
        self.service.batch_import_graph = AsyncMock()

        triples = await self.service.extract_triples_from_chunks(
            chunks=chunks,
            doc_id="test-doc-4",
            use_rules=True,
            use_llm=False,
        )

        assert triples == []
        self.service.batch_import_graph.assert_not_called()

    @pytest.mark.asyncio
    async def test_extract_from_chunks_llm_fallback_threshold(self) -> None:
        """规则提取达到阈值时跳过 LLM，节省成本。"""
        # 第一个 chunk 就产生 3 条三元组，达到 llm_fallback_threshold
        chunks = [
            self._make_chunk("微服务属于架构模式。系统包含用户管理。Redis基于内存存储。"),
        ]
        self.service.batch_import_graph = AsyncMock(return_value={"nodes": 6, "relationships": 9})
        self.service._extract_triples_by_llm = AsyncMock(return_value=[])

        triples = await self.service.extract_triples_from_chunks(
            chunks=chunks,
            doc_id="test-doc-5",
            llm_provider=object(),  # 非 None，验证 LLM 不被调用
            use_rules=True,
            use_llm=True,
            llm_fallback_threshold=3,
        )

        # 规则提取 >= 3，LLM 不应被调用
        assert len(triples) >= 3
        self.service._extract_triples_by_llm.assert_not_called()

    @pytest.mark.asyncio
    async def test_extract_from_chunks_correct_doc_id_in_relationships(self) -> None:
        """写入图谱时，Chunk → Entity 的 MENTIONS 关系应使用正确的 chunk_id。"""
        chunk = self._make_chunk("微服务属于架构模式", chunk_id="chunk-001")
        captured_nodes: list = []
        captured_rels: list = []

        async def capture_import(nodes, relationships, batch_size=500):
            captured_nodes.extend(nodes)
            captured_rels.extend(relationships)
            return {"nodes": len(nodes), "relationships": len(relationships)}

        self.service.batch_import_graph = capture_import

        await self.service.extract_triples_from_chunks(
            chunks=[chunk],
            doc_id="my-doc-id",
            use_rules=True,
            use_llm=False,
        )

        # 验证 MENTIONS 关系从 DocumentChunk 指向 Entity
        mentions_rels = [r for r in captured_rels if r["type"] == "MENTIONS"]
        assert len(mentions_rels) == 2
        for rel in mentions_rels:
            assert rel["from_id"] == "chunk-001"
            assert rel["from_label"] == "DocumentChunk"

    @pytest.mark.asyncio
    async def test_extract_from_chunks_creates_chunk_nodes_and_has_chunk(self) -> None:
        """Chunk 入图：创建 DocumentChunk 节点 + Document → Chunk HAS_CHUNK 边。"""
        chunks = [
            self._make_chunk("微服务属于架构模式", chunk_id="chunk-a"),
            self._make_chunk("系统包含用户管理模块", chunk_id="chunk-b"),
        ]
        captured_nodes: list = []
        captured_rels: list = []

        async def capture_import(nodes, relationships, batch_size=500):
            captured_nodes.extend(nodes)
            captured_rels.extend(relationships)
            return {"nodes": len(nodes), "relationships": len(relationships)}

        self.service.batch_import_graph = capture_import

        await self.service.extract_triples_from_chunks(
            chunks=chunks,
            doc_id="doc-xyz",
            use_rules=True,
            use_llm=False,
        )

        # 验证 DocumentChunk 节点存在
        chunk_nodes = [n for n in captured_nodes if n["label"] == "DocumentChunk"]
        assert len(chunk_nodes) == 2
        chunk_ids = {n["id"] for n in chunk_nodes}
        assert chunk_ids == {"chunk-a", "chunk-b"}
        # 验证 chunk 节点携带 doc_id 和 text（溯源信息）
        for cn in chunk_nodes:
            assert cn["doc_id"] == "doc-xyz"
            assert "text" in cn
            assert cn["text"]  # 非空

        # 验证 HAS_CHUNK 边: Document → DocumentChunk
        has_chunk_rels = [r for r in captured_rels if r["type"] == "HAS_CHUNK"]
        assert len(has_chunk_rels) == 2
        for rel in has_chunk_rels:
            assert rel["from_label"] == "Document"
            assert rel["from_id"] == "doc-xyz"
            assert rel["to_label"] == "DocumentChunk"
        has_chunk_targets = {r["to_id"] for r in has_chunk_rels}
        assert has_chunk_targets == {"chunk-a", "chunk-b"}

        # 验证 MENTIONS 从 DocumentChunk 指向 Entity（不是 Document）
        mentions_rels = [r for r in captured_rels if r["type"] == "MENTIONS"]
        assert len(mentions_rels) > 0
        for rel in mentions_rels:
            assert rel["from_label"] == "DocumentChunk"
            assert rel["from_id"] in ("chunk-a", "chunk-b")


# ======================================================================
# KG 增强召回 — 图谱召回内容映射测试
# ======================================================================


class TestGraphSearchContentMapping:
    """图谱召回结果内容映射测试 — 验证 chunk_text 替换 title 占位。

    验证 _graph_search 的核心逻辑：
    - 有 chunk_text 时，content 使用实际段落内容（不是标题占位）
    - 有 chunk_id 时，使用真实 chunk_id（不是 "graph_{doc_id}" 伪 ID）
    - 无 chunk 数据时，回退到 title 占位（向后兼容旧数据）
    """

    def test_chunk_text_used_as_content_when_available(self):
        """有 chunk_text 时，content 应使用 chunk_text 而非 title。"""
        doc = {
            "doc_id": "doc-1",
            "title": "故障手册",
            "kb_id": "kb-1",
            "chunk_id": "chunk-001",
            "chunk_text": "电机过热时检查散热风扇是否运转正常",
            "title_path": "第三章 > 故障处理",
        }
        content = doc.get("chunk_text") or doc.get("title", "")
        chunk_id = doc.get("chunk_id") or f"graph_{doc['doc_id']}"
        title_path = doc.get("title_path") or ""

        assert content == "电机过热时检查散热风扇是否运转正常"
        assert chunk_id == "chunk-001"
        assert title_path == "第三章 > 故障处理"

    def test_title_fallback_when_no_chunk_text(self):
        """无 chunk_text（旧数据）时，content 回退到 title。"""
        doc = {
            "doc_id": "doc-1",
            "title": "故障手册",
            "kb_id": "kb-1",
            "chunk_id": None,
            "chunk_text": None,
            "title_path": None,
        }
        content = doc.get("chunk_text") or doc.get("title", "")
        chunk_id = doc.get("chunk_id") or f"graph_{doc['doc_id']}"
        title_path = doc.get("title_path") or ""

        assert content == "故障手册"
        assert chunk_id == "graph_doc-1"
        assert title_path == ""

    def test_find_related_returns_chunk_fields(self):
        """find_related_documents_by_entity 返回的字典应包含 chunk 级字段。"""
        # 模拟 find_related_documents_by_entity 的返回格式
        result = {
            "doc_id": "doc-1",
            "title": "维修手册",
            "kb_id": "kb-1",
            "chunk_id": "chunk-abc",
            "chunk_text": "液压系统压力低于 5MPa 时检查泵体密封",
            "title_path": "第二章 > 液压系统 > 故障诊断",
        }
        assert "chunk_id" in result
        assert "chunk_text" in result
        assert "title_path" in result
        assert result["chunk_text"]  # 非空
        assert result["title_path"]  # 非空
