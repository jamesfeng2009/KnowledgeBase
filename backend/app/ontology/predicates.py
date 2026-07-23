"""
预置谓词映射 — 中文谓词到标准关系类型映射。

对齐 GraphService._RULE_PATTERNS 中的 11 个规则模板，
确保规则提取的三元组在写入图谱时使用标准英文关系类型。
"""

from __future__ import annotations

from app.ontology.entity_registry import RelationType

PREDICATE_MAPPINGS: dict[str, RelationType] = {
    # GraphService._RULE_PATTERNS 中的 11 个规则谓词
    "属于": RelationType.BELONGS_TO,
    "包含": RelationType.RELATES_TO,
    "引用": RelationType.REFERENCES,
    "替代": RelationType.REPLACES,
    "依赖": RelationType.RELATES_TO,
    "基于": RelationType.RELATES_TO,
    "使用": RelationType.RELATES_TO,
    "定义": RelationType.RELATES_TO,
    "管理": RelationType.RELATES_TO,
    "实现": RelationType.RELATES_TO,
    # 额外常见谓词
    "提及": RelationType.MENTIONS,
    "编写": RelationType.AUTHORED_BY,
    "审批": RelationType.APPROVED_BY,
    "参考": RelationType.REFERENCES,
}
