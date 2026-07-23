"""
实体注册表 — 单一职责：同义词索引 + 实体类型分类 + 谓词映射。

核心能力：
    1. resolve_entity(term) — 同义词解析："合约" → EntityDefinition(canonical="contract")
    2. normalize_triple(s, p, o) — 三元组归一化：写入 Neo4j 前标准化实体类型和关系类型
    3. expand_query(query) — 查询扩展：识别查询中的实体 + 同义词扩展

遵循开闭原则：新增实体只需调用 register()，新增谓词映射只需调用 register_predicate()。
遵循优雅降级：所有方法在实体未注册时返回 None / 默认值，不抛异常。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from app.utils.logger import get_logger

log = get_logger(__name__)


class EntityType(str, Enum):
    """标准实体类型 — 对齐 GraphService 已有声明的 6 类节点。"""

    DOCUMENT = "Document"
    CONCEPT = "Concept"
    POLICY = "Policy"
    PRODUCT = "Product"
    DEPARTMENT = "Department"
    PERSON = "Person"


class RelationType(str, Enum):
    """标准关系类型 — 对齐 GraphService 已有声明的 8 种关系。"""

    REFERENCES = "REFERENCES"
    MENTIONS = "MENTIONS"
    RELATES_TO = "RELATES_TO"
    REPLACES = "REPLACES"
    BELONGS_TO = "BELONGS_TO"
    HYPERNYM = "HYPERNYM"
    AUTHORED_BY = "AUTHORED_BY"
    APPROVED_BY = "APPROVED_BY"


@dataclass
class EntityDefinition:
    """实体定义。

    Attributes:
        canonical_name: 规范名称（英文标识符），如 "contract"。
        display_name: 显示名称（中文），如 "合同"。
        entity_type: 实体类型（EntityType 枚举）。
        synonyms: 同义词列表，如 ["合约", "协议", "contract", "agreement"]。
        description: 实体描述（可选）。
    """

    canonical_name: str
    display_name: str
    entity_type: EntityType
    synonyms: list[str] = field(default_factory=list)
    description: str = ""


@dataclass
class TripleNormalization:
    """三元组归一化结果。

    Attributes:
        subject_canonical: 归一化后的主语（规范名称）。
        subject_type: 主语实体类型。
        predicate_standard: 标准关系类型。
        object_canonical: 归一化后的宾语（规范名称）。
        object_type: 宾语实体类型。
    """

    subject_canonical: str
    subject_type: EntityType
    predicate_standard: RelationType
    object_canonical: str
    object_type: EntityType


class EntityRegistry:
    """实体注册表 — 同义词索引 + 类型分类 + 谓词映射。

    使用方式::

        from app.ontology.entity_registry import EntityRegistry, EntityType

        # 注册实体
        EntityRegistry.register(EntityDefinition(
            canonical_name="contract",
            display_name="合同",
            entity_type=EntityType.CONCEPT,
            synonyms=["合约", "协议", "contract"],
        ))

        # 同义词解析
        entity = EntityRegistry.resolve_entity("合约")  # → contract 定义

        # 三元组归一化
        result = EntityRegistry.normalize_triple("合约", "属于", "华为")
        # → TripleNormalization(subject_canonical="contract", predicate_standard=BELONGS_TO, ...)

        # 查询扩展
        terms, related = EntityRegistry.expand_query("查一下合约")
        # → (["合约", "协议", "contract", "合同"], [])
    """

    # canonical_name → EntityDefinition
    _entities: dict[str, EntityDefinition] = {}
    # synonym/alias (lowercase) → canonical_name
    _synonym_index: dict[str, str] = {}
    # 中文谓词 → RelationType
    _predicate_map: dict[str, RelationType] = {}
    # 标记是否已初始化
    _initialized: bool = False

    # ------------------------------------------------------------------
    # 注册接口
    # ------------------------------------------------------------------

    @classmethod
    def register(cls, entity: EntityDefinition) -> None:
        """注册实体定义。

        自动构建同义词索引：canonical_name / display_name / synonyms 全部映射到 canonical_name。

        Args:
            entity: 实体定义。
        """
        cls._entities[entity.canonical_name] = entity
        # 构建同义词索引（全部小写，匹配时不区分大小写）
        cls._synonym_index[entity.canonical_name.lower()] = entity.canonical_name
        cls._synonym_index[entity.display_name.lower()] = entity.canonical_name
        for syn in entity.synonyms:
            cls._synonym_index[syn.lower()] = entity.canonical_name

    @classmethod
    def register_predicate(cls, chinese_pred: str, standard: RelationType) -> None:
        """注册中文谓词到标准关系类型的映射。

        Args:
            chinese_pred: 中文谓词，如 "属于"。
            standard: 标准关系类型，如 RelationType.BELONGS_TO。
        """
        cls._predicate_map[chinese_pred] = standard

    @classmethod
    def _ensure_initialized(cls) -> None:
        """确保预置实体和谓词映射已加载（懒初始化，仅一次）。"""
        if cls._initialized:
            return
        cls._initialized = True
        try:
            from app.ontology.entities import ENTITY_DEFINITIONS
            from app.ontology.predicates import PREDICATE_MAPPINGS

            for entity_def in ENTITY_DEFINITIONS:
                cls.register(entity_def)
            for ch_pred, rel_type in PREDICATE_MAPPINGS.items():
                cls.register_predicate(ch_pred, rel_type)
            log.info(
                "entity_registry.initialized",
                entities=len(cls._entities),
                predicates=len(cls._predicate_map),
            )
        except Exception as exc:
            log.warning("entity_registry.init_failed", error=str(exc))

    # ------------------------------------------------------------------
    # 解析接口
    # ------------------------------------------------------------------

    @classmethod
    def resolve_entity(cls, term: str) -> EntityDefinition | None:
        """同义词解析 — 将任意别名解析为标准实体定义。

        Args:
            term: 实体名称或别名（如 "合约"、"contract"、"合同"）。

        Returns:
            EntityDefinition | None: 匹配的实体定义，未匹配返回 None。
        """
        cls._ensure_initialized()
        if not term or not term.strip():
            return None
        canonical = cls._synonym_index.get(term.lower().strip())
        if not canonical:
            return None
        return cls._entities.get(canonical)

    @classmethod
    def normalize_triple(
        cls,
        subject: str,
        predicate: str,
        object_: str,
    ) -> TripleNormalization:
        """归一化三元组 — 写入 Neo4j 前标准化实体类型和关系类型。

        流程：
            1. 实体归一化 — subject/object 通过同义词索引映射到 canonical_name
            2. 类型分类 — 已注册实体使用注册类型，未注册默认 Concept
            3. 谓词映射 — 中文谓词映射到标准 RelationType，未映射默认 RELATES_TO

        Args:
            subject: 主语原始文本。
            predicate: 谓词原始文本（如 "属于"）。
            object_: 宾语原始文本。

        Returns:
            TripleNormalization: 归一化结果。
        """
        cls._ensure_initialized()

        # 实体归一化
        s_def = cls.resolve_entity(subject)
        o_def = cls.resolve_entity(object_)

        s_canonical = s_def.canonical_name if s_def else subject
        s_type = s_def.entity_type if s_def else EntityType.CONCEPT
        o_canonical = o_def.canonical_name if o_def else object_
        o_type = o_def.entity_type if o_def else EntityType.CONCEPT

        # 谓词映射
        pred_standard = cls._predicate_map.get(predicate, RelationType.RELATES_TO)

        return TripleNormalization(
            subject_canonical=s_canonical,
            subject_type=s_type,
            predicate_standard=pred_standard,
            object_canonical=o_canonical,
            object_type=o_type,
        )

    @classmethod
    def expand_query(cls, query: str) -> tuple[list[str], list[str]]:
        """查询扩展 — 识别查询中的实体并返回同义词扩展词列表。

        用于检索前的实体识别 + 同义词扩展，增强 BM25 和向量检索的召回率。

        Args:
            query: 用户查询文本。

        Returns:
            tuple[同义词扩展词列表, 关联实体canonical_name列表]:
            - 同义词扩展词列表：查询中已知实体的所有同义词（用于 BM25 OR 查询）
            - 关联实体列表：查询中已识别的实体 canonical_name（用于图谱召回）
        """
        cls._ensure_initialized()

        expanded_terms: list[str] = []
        related_entities: list[str] = []
        seen_terms: set[str] = set()

        for term in cls._split_terms(query):
            entity = cls.resolve_entity(term)
            if entity and entity.canonical_name not in related_entities:
                related_entities.append(entity.canonical_name)
                # 添加同义词（去重）
                for syn in entity.synonyms:
                    if syn.lower() not in seen_terms:
                        seen_terms.add(syn.lower())
                        expanded_terms.append(syn)
                # 添加显示名称
                if entity.display_name.lower() not in seen_terms:
                    seen_terms.add(entity.display_name.lower())
                    expanded_terms.append(entity.display_name)

        return expanded_terms, related_entities

    @classmethod
    def _split_terms(cls, text: str) -> list[str]:
        """简易分词 — 2-4 字滑窗 + 英文单词。

        与 SkillFinder 的分词策略对齐，不依赖 jieba。

        Args:
            text: 待分词文本。

        Returns:
            候选词列表（从长到短排序，优先匹配长词）。
        """
        terms: list[str] = []
        # 中文 2-4 字滑窗
        for n in (4, 3, 2):
            for i in range(len(text) - n + 1):
                candidate = text[i : i + n]
                # 过滤纯标点和空白
                if candidate.strip() and not re.match(r"^[\s\W]+$", candidate):
                    terms.append(candidate)
        # 英文单词
        terms.extend(re.findall(r"[a-zA-Z_]+", text))
        return terms

    # ------------------------------------------------------------------
    # 测试辅助
    # ------------------------------------------------------------------

    @classmethod
    def _reset(cls) -> None:
        """重置注册表 — 仅供测试使用。"""
        cls._entities.clear()
        cls._synonym_index.clear()
        cls._predicate_map.clear()
        cls._initialized = False
