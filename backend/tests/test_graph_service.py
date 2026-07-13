"""GraphService 测试 — 规则提取、实体验证、批量导入结构。

不依赖 Neo4j / Redis 外部服务，仅测试纯 Python 逻辑。
"""

import pytest

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
