"""
RAG 检索质量评测 — 预置查询模板库。

预置查询按 6 种检索场景分类（参考 test.md「语义检索原理」一节的对比维度），
覆盖关键词检索 vs 语义检索的差异点，帮助用户构建多样化评测数据集。

注意（test.md 强调）：
    - 测试数据必须人工标注 ground truth，且与算法/开发团队隔离，防过拟合。
    - 因文档 ID 是用户私有的 UUID，预置模板不携带 ground_truth_doc_ids，
      用户须从自己的知识库中选择相关文档完成标注。
    - 预置模板仅作查询类型脚手架，用户可自由编辑或自定义。
"""

from __future__ import annotations

from typing import TypedDict


class PresetQuery(TypedDict):
    """预置查询模板结构。"""

    query: str
    query_type: str
    difficulty: str
    description: str


# ======================================================================
# 6 类检索场景 — 共 12 条预置查询模板
# ======================================================================

PRESET_QUERIES: list[PresetQuery] = [
    # ------------------------------------------------------------------
    # 1. 精确匹配（exact_match）— 查询包含文档中的明确关键词
    # 难点：基础召回，向量与全文检索都应命中
    # ------------------------------------------------------------------
    {
        "query": "公司的报销流程是什么？",
        "query_type": "exact_match",
        "difficulty": "easy",
        "description": "直接查询流程类文档，关键词明确，应能精准召回",
    },
    {
        "query": "年假有多少天？",
        "query_type": "exact_match",
        "difficulty": "easy",
        "description": "查询考勤/假期制度，关键词「年假」应直接匹配",
    },

    # ------------------------------------------------------------------
    # 2. 语义相似（semantic）— 用词不同但语义相同，考验 embedding 能力
    # 难点：关键词检索会失效，依赖语义理解
    # ------------------------------------------------------------------
    {
        "query": "怎么报销差旅费用？",
        "query_type": "semantic",
        "difficulty": "medium",
        "description": "「差旅费用」与「差旅费」「交通费」语义相近，考验同义召回",
    },
    {
        "query": "员工离职需要办理哪些手续？",
        "query_type": "semantic",
        "difficulty": "medium",
        "description": "「离职手续」与「离职流程」「交接」语义关联",
    },

    # ------------------------------------------------------------------
    # 3. 同义词/近义词（synonym）— 用近义词替换原词
    # 难点：考验 embedding 对同义词的泛化
    # ------------------------------------------------------------------
    {
        "query": "新员工入职指引",
        "query_type": "synonym",
        "difficulty": "medium",
        "description": "「入职指引」与「入职指南」「onboarding」近义",
    },
    {
        "query": "信息安全规范",
        "query_type": "synonym",
        "difficulty": "medium",
        "description": "「信息安全」与「数据安全」「网络安全」近义关联",
    },

    # ------------------------------------------------------------------
    # 4. 跨语言（cross_lingual）— 中英混合查询
    # 难点：关键词检索完全失效，依赖跨语言 embedding
    # ------------------------------------------------------------------
    {
        "query": "API 接口的调用方式是什么？",
        "query_type": "cross_lingual",
        "difficulty": "hard",
        "description": "中英混合，需理解 API/接口/调用 的跨语言语义",
    },
    {
        "query": "How to apply for leave?",
        "query_type": "cross_lingual",
        "difficulty": "hard",
        "description": "纯英文查询中文知识库，考验跨语言检索能力",
    },

    # ------------------------------------------------------------------
    # 5. 模糊/口语化（fuzzy）— 口语化、不完整表述
    # 难点：考验对口语化查询的语义理解
    # ------------------------------------------------------------------
    {
        "query": "请假怎么弄",
        "query_type": "fuzzy",
        "difficulty": "hard",
        "description": "口语化、无标点，需理解「请假」意图并召回流程文档",
    },
    {
        "query": "报销找谁签字",
        "query_type": "fuzzy",
        "difficulty": "hard",
        "description": "口语化查询审批节点，考验语义意图理解",
    },

    # ------------------------------------------------------------------
    # 6. 多约束（multi_constraint）— 含多个条件的复合查询
    # 难点：需同时满足多个约束，考验综合召回与排序
    # ------------------------------------------------------------------
    {
        "query": "2024年差旅费报销标准和审批流程",
        "query_type": "multi_constraint",
        "difficulty": "hard",
        "description": "同时约束「2024年」「差旅费」「标准」「审批流程」，需综合召回",
    },
    {
        "query": "试用期内员工的年假和病假规定",
        "query_type": "multi_constraint",
        "difficulty": "hard",
        "description": "多约束复合：试用期 + 年假 + 病假，考验多条件召回",
    },
]


def get_preset_queries() -> list[PresetQuery]:
    """获取全部预置查询模板。"""
    return PRESET_QUERIES.copy()


def get_query_type_summary() -> dict[str, int]:
    """按查询类型统计模板数量。"""
    summary: dict[str, int] = {}
    for q in PRESET_QUERIES:
        qt = q["query_type"]
        summary[qt] = summary.get(qt, 0) + 1
    return summary


# 查询类型中文名映射（供前端展示）
QUERY_TYPE_NAMES: dict[str, str] = {
    "exact_match": "精确匹配",
    "semantic": "语义相似",
    "synonym": "同义词",
    "cross_lingual": "跨语言",
    "fuzzy": "模糊口语",
    "multi_constraint": "多约束",
}
