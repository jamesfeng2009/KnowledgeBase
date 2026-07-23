"""P2 EntityRegistry 企业本体单元测试。

覆盖：
- 实体注册与同义词解析
- 三元组归一化（实体类型 + 谓词映射）
- 查询扩展（同义词 + 关联实体识别）
- 预置实体和谓词映射
- 优雅降级
"""
import pytest

from app.ontology.entity_registry import (
    EntityDefinition,
    EntityRegistry,
    EntityType,
    RelationType,
    TripleNormalization,
)


class TestEntityRegistryBasic:
    """EntityRegistry 基础功能测试。"""

    def setup_method(self):
        EntityRegistry._reset()

    def test_register_and_resolve(self):
        """注册实体后可通过同义词解析。"""
        EntityRegistry.register(EntityDefinition(
            canonical_name="contract",
            display_name="合同",
            entity_type=EntityType.CONCEPT,
            synonyms=["合约", "协议", "contract"],
        ))

        # 通过规范名解析
        entity = EntityRegistry.resolve_entity("contract")
        assert entity is not None
        assert entity.canonical_name == "contract"

        # 通过显示名解析
        entity = EntityRegistry.resolve_entity("合同")
        assert entity is not None
        assert entity.canonical_name == "contract"

        # 通过同义词解析
        entity = EntityRegistry.resolve_entity("合约")
        assert entity is not None
        assert entity.canonical_name == "contract"

        # 通过英文同义词解析
        entity = EntityRegistry.resolve_entity("contract")
        assert entity is not None
        assert entity.canonical_name == "contract"

    def test_resolve_unknown_entity(self):
        """未注册的实体返回 None。"""
        EntityRegistry._reset()
        entity = EntityRegistry.resolve_entity("未知实体")
        assert entity is None

    def test_resolve_empty_term(self):
        """空字符串返回 None。"""
        assert EntityRegistry.resolve_entity("") is None
        assert EntityRegistry.resolve_entity("   ") is None
        assert EntityRegistry.resolve_entity(None) is None

    def test_case_insensitive(self):
        """同义词匹配不区分大小写。"""
        EntityRegistry.register(EntityDefinition(
            canonical_name="product",
            display_name="产品",
            entity_type=EntityType.PRODUCT,
            synonyms=["Product", "ITEM"],
        ))

        assert EntityRegistry.resolve_entity("product") is not None
        assert EntityRegistry.resolve_entity("PRODUCT") is not None
        assert EntityRegistry.resolve_entity("Product") is not None
        assert EntityRegistry.resolve_entity("item") is not None
        assert EntityRegistry.resolve_entity("ITEM") is not None


class TestNormalizeTriple:
    """三元组归一化测试。"""

    def setup_method(self):
        EntityRegistry._reset()
        EntityRegistry.register(EntityDefinition(
            canonical_name="contract",
            display_name="合同",
            entity_type=EntityType.CONCEPT,
            synonyms=["合约", "协议"],
        ))
        EntityRegistry.register(EntityDefinition(
            canonical_name="customer",
            display_name="客户",
            entity_type=EntityType.PERSON,
            synonyms=["甲方"],
        ))
        EntityRegistry.register_predicate("属于", RelationType.BELONGS_TO)
        EntityRegistry.register_predicate("引用", RelationType.REFERENCES)

    def test_normalize_with_registered_entities(self):
        """已注册实体的归一化。"""
        result = EntityRegistry.normalize_triple("合约", "属于", "甲方")
        assert result.subject_canonical == "contract"
        assert result.subject_type == EntityType.CONCEPT
        assert result.predicate_standard == RelationType.BELONGS_TO
        assert result.object_canonical == "customer"
        assert result.object_type == EntityType.PERSON

    def test_normalize_with_unregistered_entities(self):
        """未注册实体默认 Concept 类型。"""
        result = EntityRegistry.normalize_triple("新概念", "引用", "另一个概念")
        assert result.subject_canonical == "新概念"
        assert result.subject_type == EntityType.CONCEPT
        assert result.predicate_standard == RelationType.REFERENCES
        assert result.object_canonical == "另一个概念"
        assert result.object_type == EntityType.CONCEPT

    def test_normalize_unmapped_predicate(self):
        """未映射的谓词默认 RELATES_TO。"""
        result = EntityRegistry.normalize_triple("实体A", "新关系", "实体B")
        assert result.predicate_standard == RelationType.RELATES_TO

    def test_normalize_triple_type(self):
        """归一化结果类型正确。"""
        result = EntityRegistry.normalize_triple("A", "属于", "B")
        assert isinstance(result, TripleNormalization)


class TestExpandQuery:
    """查询扩展测试。"""

    def setup_method(self):
        EntityRegistry._reset()
        EntityRegistry.register(EntityDefinition(
            canonical_name="contract",
            display_name="合同",
            entity_type=EntityType.CONCEPT,
            synonyms=["合约", "协议", "contract"],
        ))
        EntityRegistry.register(EntityDefinition(
            canonical_name="payment",
            display_name="回款",
            entity_type=EntityType.CONCEPT,
            synonyms=["收款", "payment"],
        ))

    def test_expand_with_known_entity(self):
        """查询包含已知实体时返回扩展词。"""
        expanded, related = EntityRegistry.expand_query("查一下合同")
        assert len(expanded) > 0
        assert "合同" in expanded or "合约" in expanded or "协议" in expanded
        assert "contract" in related

    def test_expand_with_synonym(self):
        """查询包含同义词时也能识别。"""
        expanded, related = EntityRegistry.expand_query("查一下合约")
        assert len(expanded) > 0
        assert "contract" in related

    def test_expand_multiple_entities(self):
        """查询包含多个已知实体时全部识别。"""
        expanded, related = EntityRegistry.expand_query("合同的回款")
        assert "contract" in related
        assert "payment" in related

    def test_expand_no_known_entities(self):
        """查询不含已知实体时返回空列表。"""
        expanded, related = EntityRegistry.expand_query("今天天气不错")
        assert expanded == []
        assert related == []

    def test_expand_empty_query(self):
        """空查询返回空列表。"""
        expanded, related = EntityRegistry.expand_query("")
        assert expanded == []
        assert related == []


class TestPresetEntities:
    """预置实体和谓词映射测试。"""

    def test_preset_entities_loaded(self):
        """预置实体在首次访问时自动加载。"""
        EntityRegistry._reset()
        # 触发懒初始化
        entity = EntityRegistry.resolve_entity("合同")
        assert entity is not None
        assert entity.canonical_name == "contract"

    def test_preset_contract_synonyms(self):
        """预置合同实体同义词正确。"""
        EntityRegistry._reset()
        for syn in ["合同", "合约", "协议", "contract", "agreement"]:
            entity = EntityRegistry.resolve_entity(syn)
            assert entity is not None, f"同义词 '{syn}' 未解析到 contract"
            assert entity.canonical_name == "contract"

    def test_preset_payment_synonyms(self):
        """预置回款实体同义词正确。"""
        EntityRegistry._reset()
        for syn in ["回款", "收款", "付款", "payment", "payment_received"]:
            entity = EntityRegistry.resolve_entity(syn)
            assert entity is not None, f"同义词 '{syn}' 未解析到 payment"
            assert entity.canonical_name == "payment"

    def test_preset_predicate_mappings(self):
        """预置谓词映射正确。"""
        EntityRegistry._reset()
        # 触发初始化
        EntityRegistry.resolve_entity("合同")

        result = EntityRegistry.normalize_triple("合同", "属于", "部门")
        assert result.predicate_standard == RelationType.BELONGS_TO

        result = EntityRegistry.normalize_triple("文档A", "引用", "文档B")
        assert result.predicate_standard == RelationType.REFERENCES

        result = EntityRegistry.normalize_triple("旧制度", "替代", "新制度")
        assert result.predicate_standard == RelationType.REPLACES

    def test_entity_types_diverse(self):
        """预置实体覆盖多种实体类型。"""
        EntityRegistry._reset()
        # Concept
        assert EntityRegistry.resolve_entity("合同").entity_type == EntityType.CONCEPT
        # Person
        assert EntityRegistry.resolve_entity("客户").entity_type == EntityType.PERSON
        # Product
        assert EntityRegistry.resolve_entity("产品").entity_type == EntityType.PRODUCT
        # Policy
        assert EntityRegistry.resolve_entity("政策").entity_type == EntityType.POLICY
        # Department
        assert EntityRegistry.resolve_entity("部门").entity_type == EntityType.DEPARTMENT


class TestEntityTypeAndRelation:
    """EntityType / RelationType 枚举测试。"""

    def test_entity_type_values(self):
        """EntityType 枚举值与 GraphService 声明一致。"""
        assert EntityType.DOCUMENT.value == "Document"
        assert EntityType.CONCEPT.value == "Concept"
        assert EntityType.POLICY.value == "Policy"
        assert EntityType.PRODUCT.value == "Product"
        assert EntityType.DEPARTMENT.value == "Department"
        assert EntityType.PERSON.value == "Person"

    def test_relation_type_values(self):
        """RelationType 枚举值与 GraphService 声明一致。"""
        assert RelationType.REFERENCES.value == "REFERENCES"
        assert RelationType.MENTIONS.value == "MENTIONS"
        assert RelationType.BELONGS_TO.value == "BELONGS_TO"
        assert RelationType.REPLACES.value == "REPLACES"
        assert RelationType.HYPERNYM.value == "HYPERNYM"
        assert RelationType.AUTHORED_BY.value == "AUTHORED_BY"
        assert RelationType.APPROVED_BY.value == "APPROVED_BY"
        assert RelationType.RELATES_TO.value == "RELATES_TO"
