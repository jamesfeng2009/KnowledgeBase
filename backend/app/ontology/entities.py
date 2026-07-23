"""
预置实体定义 — 企业常见实体。

可通过 EntityRegistry.register() 动态扩展，也可通过 API（未来）管理。
覆盖企业知识库最常见的 7 个核心实体，支持中英文同义词。
"""

from __future__ import annotations

from app.ontology.entity_registry import EntityDefinition, EntityType

ENTITY_DEFINITIONS: list[EntityDefinition] = [
    EntityDefinition(
        canonical_name="contract",
        display_name="合同",
        entity_type=EntityType.CONCEPT,
        synonyms=["合约", "协议", "contract", "agreement"],
        description="企业签订的合同或协议文件",
    ),
    EntityDefinition(
        canonical_name="customer",
        display_name="客户",
        entity_type=EntityType.PERSON,
        synonyms=["客户", "甲方", "customer", "client", "顾客"],
        description="企业的客户或合作方",
    ),
    EntityDefinition(
        canonical_name="product",
        display_name="产品",
        entity_type=EntityType.PRODUCT,
        synonyms=["产品", "商品", "product", "item"],
        description="企业的产品或服务",
    ),
    EntityDefinition(
        canonical_name="policy",
        display_name="政策",
        entity_type=EntityType.POLICY,
        synonyms=["政策", "制度", "规定", "policy", "regulation", "规章"],
        description="企业内部政策、制度或规定",
    ),
    EntityDefinition(
        canonical_name="department",
        display_name="部门",
        entity_type=EntityType.DEPARTMENT,
        synonyms=["部门", "科室", "department", "division", "团队"],
        description="企业内部组织部门",
    ),
    EntityDefinition(
        canonical_name="invoice",
        display_name="发票",
        entity_type=EntityType.CONCEPT,
        synonyms=["发票", "票据", "invoice", "receipt"],
        description="企业开具或收到的发票",
    ),
    EntityDefinition(
        canonical_name="payment",
        display_name="回款",
        entity_type=EntityType.CONCEPT,
        synonyms=["回款", "收款", "付款", "payment", "payment_received", "打款"],
        description="企业的收款或付款记录",
    ),
]
