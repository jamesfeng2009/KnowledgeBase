"""
AI 评测 Schema — 单一职责：定义 Prompt Injection 测试与 RAG 检索质量评测的请求/响应数据结构。

遵循分层架构：本模块仅做数据验证和序列化，业务逻辑由对应 Service 处理。
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.common import PaginationParams


# ======================================================================
# 测试套件
# ======================================================================


class InjectionSuiteCreate(BaseModel):
    """创建测试套件请求。"""

    name: str = Field(..., min_length=1, max_length=255, description="套件名称")
    description: str | None = Field(None, description="套件描述")
    target_mode: str = Field(
        "system_prompt", description="测试目标: system_prompt/full_rag"
    )
    kb_ids: list[str] | None = Field(None, description="关联知识库 ID 列表（full_rag 模式）")


class InjectionSuiteResponse(BaseModel):
    """测试套件响应。"""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    description: str | None = None
    target_mode: str = "system_prompt"
    kb_ids: list[str] | None = None
    status: str = "created"
    total_cases: int = 0
    passed_count: int = 0
    partial_count: int = 0
    failed_count: int = 0
    duration_seconds: int = 0
    created_at: datetime | None = None
    updated_at: datetime | None = None


# ======================================================================
# 攻击用例
# ======================================================================


class InjectionCaseResponse(BaseModel):
    """攻击用例响应。"""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    suite_id: UUID
    attack_type: str
    severity: str
    title: str
    prompt: str
    expected_behavior: str
    attack_target: str | None = None
    source: str = "preset"


class InjectionCaseResultResponse(BaseModel):
    """单条用例执行结果响应（合并用例 + 结果）。"""

    model_config = ConfigDict(from_attributes=True)

    case_id: UUID
    attack_type: str
    severity: str
    title: str
    prompt: str
    expected_behavior: str
    attack_target: str | None = None
    # 结果
    response_text: str | None = None
    verdict: str = "pending"
    checks: dict | None = None
    score_reason: str | None = None
    response_time: int = 0
    error_message: str | None = None
    executed_at: datetime | None = None


# ======================================================================
# 执行请求
# ======================================================================


class InjectionRunRequest(BaseModel):
    """执行 Prompt Injection 测试请求。"""

    suite_id: UUID = Field(..., description="套件 ID")
    # 可选：只执行指定类型的用例
    attack_types: list[str] | None = Field(
        None, description="只执行指定攻击类型（空则全部执行）"
    )


# ======================================================================
# 统计
# ======================================================================


class InjectionStatsResponse(BaseModel):
    """Prompt Injection 测试统计。"""

    total_suites: int = 0
    total_cases: int = 0
    total_executed: int = 0
    total_passed: int = 0
    total_partial: int = 0
    total_failed: int = 0
    # 按攻击类型统计通过率
    by_attack_type: dict[str, dict] = Field(
        default_factory=dict, description="按攻击类型统计"
    )
    # 防御得分（0-100，pass=100, partial=50, fail=0 的加权平均）
    defense_score: float = 0.0


# ======================================================================
# RAG 检索质量评测
# ======================================================================


class RagDatasetCreate(BaseModel):
    """创建 RAG 评测数据集请求。"""

    name: str = Field(..., min_length=1, max_length=255, description="数据集名称")
    description: str | None = Field(None, description="数据集描述")
    kb_ids: list[str] | None = Field(
        None, description="关联知识库 ID 列表（限定检索范围）"
    )
    top_k: int = Field(5, ge=1, le=50, description="默认检索 top_k")


class RagDatasetResponse(BaseModel):
    """RAG 评测数据集响应。"""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    description: str | None = None
    kb_ids: list[str] | None = None
    top_k: int = 5
    status: str = "created"
    total_queries: int = 0
    hit_count: int = 0
    metrics: dict | None = None
    duration_seconds: int = 0
    created_at: datetime | None = None
    updated_at: datetime | None = None


class RagQueryCreate(BaseModel):
    """添加评测查询请求。"""

    query: str = Field(..., min_length=1, description="查询文本")
    query_type: str = Field(
        "semantic",
        description="查询类型: exact_match/semantic/synonym/"
        "cross_lingual/fuzzy/multi_constraint",
    )
    difficulty: str = Field("medium", description="难度: easy/medium/hard")
    ground_truth_doc_ids: list[str] = Field(
        ..., min_length=1, description="人工标注的相关文档 ID 列表"
    )
    expected_answer: str | None = Field(None, description="期望答案（可选）")


class RagQueryResponse(BaseModel):
    """评测查询响应。"""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    dataset_id: UUID
    query: str
    query_type: str = "semantic"
    difficulty: str = "medium"
    ground_truth_doc_ids: list[str] | None = None
    expected_answer: str | None = None
    source: str = "custom"
    created_at: datetime | None = None


class RagQueryResultResponse(BaseModel):
    """单条查询的检索结果与指标响应（合并查询 + 结果）。"""

    model_config = ConfigDict(from_attributes=True)

    query_id: UUID
    query: str
    query_type: str = "semantic"
    difficulty: str = "medium"
    ground_truth_doc_ids: list[str] | None = None
    # 检索结果
    retrieved: list[dict] | None = None
    # 质量指标
    metrics: dict | None = None
    retrieved_count: int = 0
    response_time_ms: int = 0
    error_message: str | None = None
    executed_at: datetime | None = None


class RagRunRequest(BaseModel):
    """执行 RAG 评测请求。"""

    dataset_id: UUID = Field(..., description="数据集 ID")
    top_k: int | None = Field(
        None, ge=1, le=50, description="检索 top_k（空则用数据集默认值）"
    )


class RagStatsResponse(BaseModel):
    """RAG 评测全局统计。"""

    total_datasets: int = 0
    total_queries: int = 0
    total_executed: int = 0
    # 全局平均指标（所有已执行数据集的加权平均）
    avg_recall_at_5: float = 0.0
    avg_mrr: float = 0.0
    avg_ndcg_at_5: float = 0.0
    avg_map: float = 0.0
    # 按查询类型统计
    by_query_type: dict[str, dict] = Field(
        default_factory=dict, description="按查询类型统计"
    )
    # 预置查询模板信息
    preset_query_count: int = 0
    preset_query_types: dict[str, int] = Field(
        default_factory=dict, description="预置查询按类型统计"
    )


# ======================================================================
# 文档解析评测
# ======================================================================


class DocParseDatasetCreate(BaseModel):
    """创建文档解析评测数据集请求。"""

    name: str = Field(..., min_length=1, max_length=255)
    description: str | None = None


class DocParseDatasetResponse(BaseModel):
    """文档解析评测数据集响应。"""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    description: str | None = None
    status: str = "created"
    total_cases: int = 0
    metrics: dict | None = None
    duration_seconds: int = 0
    created_at: datetime | None = None


class DocParseCaseCreate(BaseModel):
    """添加解析评测用例请求。"""

    title: str = Field(..., min_length=1, max_length=500)
    doc_type: str = Field("pdf", description="文档类型: pdf/docx/pptx/xlsx/html/image")
    difficulty: str = Field("medium", description="难度: easy/medium/hard")
    expected_text: str = Field(..., min_length=1, description="人工标注的标准答案文本")
    document_id: UUID | None = Field(None, description="关联文档 ID（Docling 端到端模式）")
    parsed_text: str | None = Field(None, description="待评测的解析结果文本（直接提供模式）")


class DocParseCaseResponse(BaseModel):
    """解析评测用例响应。"""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    dataset_id: UUID
    title: str
    doc_type: str = "pdf"
    difficulty: str = "medium"
    expected_text: str
    document_id: UUID | None = None
    parsed_text: str | None = None
    source: str = "custom"
    created_at: datetime | None = None


class DocParseRunRequest(BaseModel):
    """执行文档解析评测请求。"""

    dataset_id: UUID


class DocParseStatsResponse(BaseModel):
    """文档解析评测全局统计。"""

    total_datasets: int = 0
    total_cases: int = 0
    total_executed: int = 0
    avg_text_similarity: float = 0.0
    avg_cer: float = 0.0
    avg_table_score: float = 0.0
    avg_formula_score: float = 0.0
    avg_layout_score: float = 0.0
    avg_overall_score: float = 0.0


# ======================================================================
# AI Judge 自动评测
# ======================================================================


class JudgeDatasetCreate(BaseModel):
    """创建 Judge 评测数据集请求。"""

    name: str = Field(..., min_length=1, max_length=255)
    description: str | None = None
    judge_model: str = Field("default", description="裁判模型标识")
    dimensions: list[str] | None = Field(
        None, description="评分维度列表（默认 accuracy/completeness/relevance/clarity/safety）"
    )


class JudgeDatasetResponse(BaseModel):
    """Judge 评测数据集响应。"""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    description: str | None = None
    judge_model: str = "default"
    dimensions: list[str] | None = None
    status: str = "created"
    total_cases: int = 0
    metrics: dict | None = None
    duration_seconds: int = 0
    created_at: datetime | None = None


class JudgeCaseCreate(BaseModel):
    """添加 Judge 评测用例请求。"""

    question: str = Field(..., min_length=1)
    reference_answer: str = Field(..., min_length=1)
    model_answer: str = Field(..., min_length=1)
    category: str = Field("qa", description="场景类别: instruction/qa/reasoning/code/roleplay/safety")


class JudgeCaseBatchCreate(BaseModel):
    """批量添加 Judge 用例请求。"""

    cases: list[JudgeCaseCreate] = Field(..., min_length=1)


class JudgeCaseResponse(BaseModel):
    """Judge 评测用例响应。"""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    dataset_id: UUID
    question: str
    reference_answer: str
    model_answer: str
    category: str = "qa"
    source: str = "custom"
    created_at: datetime | None = None


class JudgeRunRequest(BaseModel):
    """执行 Judge 评测请求。"""

    dataset_id: UUID


class JudgeStatsResponse(BaseModel):
    """Judge 评测全局统计。"""

    total_datasets: int = 0
    total_cases: int = 0
    total_executed: int = 0
    avg_overall: float = 0.0
    dimension_averages: dict[str, float] = Field(default_factory=dict)
    default_dimensions: list[str] = Field(default_factory=list)
    dimension_names: dict[str, str] = Field(default_factory=dict)
    category_names: dict[str, str] = Field(default_factory=dict)
