"""
AI 评测模型 — 单一职责：定义 Prompt Injection 测试套件、攻击用例、执行结果表，
RAG 检索质量评测数据集、查询、检索结果表，文档解析评测与 AI Judge 自动评测表。

核心流程：
    [Prompt Injection]
        创建测试套件(InjectionTestSuite) → 导入攻击用例(InjectionTestCase)
        → 执行测试(InjectionTestResult) → 查看统计
    [RAG 检索质量评测]
        创建数据集(RagEvalDataset) → 添加查询+标注(RagEvalQuery)
        → 执行检索评测(RagEvalResult) → 查看指标(Recall@K/MRR/NDCG/MAP)
    [文档解析评测]
        创建数据集(DocParseDataset) → 添加用例+标注(DocParseCase)
        → 执行解析评测(DocParseResult) → 查看指标(文本/表格/公式/版面)
    [AI Judge 自动评测]
        创建数据集(JudgeDataset) → 添加用例(JudgeCase)
        → 执行裁判评测(JudgeResult) → 查看 LLM 多维评分

复用现有能力：
    - Base/TimestampMixin/UUIDMixin/SoftDeleteMixin：与所有业务表一致
    - 多租户：tenant_id 字段预留
    - 用户关联：created_by 关联 users 表
    - 检索执行：复用 app.rag.retriever.HybridRetriever（生产级混合检索）
    - 文档解析：复用 app.document.docling_parser.DoclingParser + app.utils.minio_client
    - LLM 裁判：复用 app.llm.factory.get_llm_provider
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, SoftDeleteMixin, TimestampMixin, UUIDMixin


# ======================================================================
# 测试套件 — 一次 Prompt Injection 防御测试的顶层容器
# ======================================================================


class InjectionTestSuite(UUIDMixin, TimestampMixin, SoftDeleteMixin, Base):
    """Prompt Injection 测试套件表 — 一次完整的防御测试运行。

    生命周期：created → running → completed / failed
    """

    __tablename__ = "ai_eval_injection_suites"

    name: Mapped[str] = mapped_column(
        String(255), nullable=False, comment="套件名称"
    )
    description: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="套件描述"
    )
    # 测试目标：system_prompt（仅测系统提示词防御）/ full_rag（测完整RAG链路）
    target_mode: Mapped[str] = mapped_column(
        String(20), default="system_prompt",
        comment="测试目标: system_prompt/full_rag",
    )
    # 关联知识库 ID（full_rag 模式下使用）
    kb_ids: Mapped[list | None] = mapped_column(
        JSONB, nullable=True, comment="关联知识库 ID 列表（full_rag 模式）"
    )
    status: Mapped[str] = mapped_column(
        String(20), default="created",
        comment="状态: created/running/completed/failed",
    )
    # 汇总统计
    total_cases: Mapped[int] = mapped_column(
        Integer, default=0, comment="用例总数"
    )
    passed_count: Mapped[int] = mapped_column(
        Integer, default=0, comment="完美防御数"
    )
    partial_count: Mapped[int] = mapped_column(
        Integer, default=0, comment="部分防御数"
    )
    failed_count: Mapped[int] = mapped_column(
        Integer, default=0, comment="防御失败数"
    )
    # 执行耗时（秒）
    duration_seconds: Mapped[int] = mapped_column(
        Integer, default=0, comment="执行耗时（秒）"
    )
    created_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False, comment="创建者 ID"
    )
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True, comment="租户 ID（多租户隔离）"
    )

    # 关系
    cases: Mapped[list["InjectionTestCase"]] = relationship(
        back_populates="suite", cascade="all, delete-orphan"
    )


# ======================================================================
# 攻击用例 — 单条 Prompt Injection 攻击定义
# ======================================================================


class InjectionTestCase(UUIDMixin, TimestampMixin, Base):
    """攻击用例表 — 一条 Prompt Injection 攻击的定义。

    来源：preset（预置用例库）/ custom（用户自定义）
    """

    __tablename__ = "ai_eval_injection_cases"

    suite_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("ai_eval_injection_suites.id"),
        nullable=False, comment="所属套件 ID"
    )
    # 攻击类型：basic_jailbreak / role_playing / system_override /
    # context_poisoning / multi_turn / encoding_obfuscation /
    # indirect_injection / combined
    attack_type: Mapped[str] = mapped_column(
        String(40), nullable=False,
        comment="攻击类型: basic_jailbreak/role_playing/system_override/"
                "context_poisoning/multi_turn/encoding_obfuscation/"
                "indirect_injection/combined",
    )
    # 严重程度：medium / high / critical
    severity: Mapped[str] = mapped_column(
        String(20), default="high",
        comment="严重程度: medium/high/critical",
    )
    title: Mapped[str] = mapped_column(
        String(500), nullable=False, comment="用例标题"
    )
    # 攻击提示词（多轮攻击时为 JSON 数组，每项为单轮提示词）
    prompt: Mapped[str] = mapped_column(
        Text, nullable=False, comment="攻击提示词"
    )
    # 期望行为描述（用于辅助评分）
    expected_behavior: Mapped[str] = mapped_column(
        Text, nullable=False, comment="期望行为：模型应拒绝执行并给出安全提示"
    )
    # 攻击目标（期望模型不应输出的内容关键词）
    attack_target: Mapped[str | None] = mapped_column(
        Text, nullable=True,
        comment="攻击目标关键词（用于检测模型是否泄露了敏感内容）",
    )
    # 来源：preset / custom
    source: Mapped[str] = mapped_column(
        String(20), default="preset", comment="来源: preset/custom"
    )

    # 关系
    suite: Mapped[InjectionTestSuite] = relationship(back_populates="cases")
    result: Mapped["InjectionTestResult | None"] = relationship(
        back_populates="case", uselist=False, cascade="all, delete-orphan"
    )


# ======================================================================
# 执行结果 — 单条攻击用例的执行与评分
# ======================================================================


class InjectionTestResult(UUIDMixin, TimestampMixin, Base):
    """执行结果表 — 一条攻击用例的执行结果与评分。

    评分等级：pass（完美防御）/ partial（部分防御）/ fail（防御失败）
    """

    __tablename__ = "ai_eval_injection_results"

    case_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("ai_eval_injection_cases.id"),
        nullable=False, comment="关联用例 ID"
    )
    suite_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("ai_eval_injection_suites.id"),
        nullable=False, comment="所属套件 ID"
    )
    # 模型响应文本
    response_text: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="模型响应文本"
    )
    # 评分等级：pass / partial / fail
    verdict: Mapped[str] = mapped_column(
        String(20), default="pending",
        comment="评分等级: pass/partial/fail",
    )
    # 5 维度检查结果（JSONB）
    # { identification: bool, refusal: bool, explanation: bool,
    #   no_leak: bool, consistency: bool }
    checks: Mapped[dict | None] = mapped_column(
        JSONB, nullable=True, comment="5 维度检查结果"
    )
    # 评分说明
    score_reason: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="评分说明"
    )
    # 响应耗时（秒）
    response_time: Mapped[int] = mapped_column(
        Integer, default=0, comment="响应耗时（秒）"
    )
    # 错误信息（执行异常时）
    error_message: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="执行错误信息"
    )
    executed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="执行时间"
    )

    # 关系
    case: Mapped[InjectionTestCase] = relationship(back_populates="result")


# ======================================================================
# RAG 检索质量评测 — 数据集 / 查询 / 检索结果
# 参考 test.md 第六/七部分：语义检索原理、余弦相似度、Recall@K / MAP。
# 评测对象：HybridRetriever（向量+全文混合检索）的召回质量。
# 标注方式：ground_truth_doc_ids 为人工标注的相关文档 ID 列表
#           （test.md 强调测试数据须人工标注且与算法隔离，防过拟合）。
# ======================================================================


class RagEvalDataset(UUIDMixin, TimestampMixin, SoftDeleteMixin, Base):
    """RAG 检索质量评测数据集表 — 一次完整检索评测的顶层容器。

    生命周期：created → running → completed / failed

    聚合指标（metrics JSONB）在 run_dataset 完成后回填，含：
        recall_at_1/3/5、precision_at_1/3/5、mrr、ndcg_at_5、map、hit_rate
    """

    __tablename__ = "ai_eval_rag_datasets"

    name: Mapped[str] = mapped_column(
        String(255), nullable=False, comment="数据集名称"
    )
    description: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="数据集描述"
    )
    # 限定检索的知识库 ID 列表（传给 HybridRetriever.search 的 kb_ids）
    kb_ids: Mapped[list | None] = mapped_column(
        JSONB, nullable=True, comment="关联知识库 ID 列表（限定检索范围）"
    )
    # 默认 top_k：检索召回数量上限
    top_k: Mapped[int] = mapped_column(
        Integer, default=5, comment="默认检索 top_k"
    )
    status: Mapped[str] = mapped_column(
        String(20), default="created",
        comment="状态: created/running/completed/failed",
    )
    # 汇总统计
    total_queries: Mapped[int] = mapped_column(
        Integer, default=0, comment="评测查询总数"
    )
    hit_count: Mapped[int] = mapped_column(
        Integer, default=0, comment="命中的查询数（top_k 内含相关文档）"
    )
    # 聚合指标 JSONB：{recall_at_1, recall_at_3, recall_at_5,
    #   precision_at_5, mrr, ndcg_at_5, map, hit_rate}
    metrics: Mapped[dict | None] = mapped_column(
        JSONB, nullable=True, comment="聚合检索质量指标"
    )
    # 执行耗时（秒）
    duration_seconds: Mapped[int] = mapped_column(
        Integer, default=0, comment="执行耗时（秒）"
    )
    created_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False, comment="创建者 ID"
    )
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True, comment="租户 ID（多租户隔离）"
    )

    # 关系
    queries: Mapped[list["RagEvalQuery"]] = relationship(
        back_populates="dataset", cascade="all, delete-orphan"
    )


class RagEvalQuery(UUIDMixin, TimestampMixin, Base):
    """评测查询表 — 单条查询及其人工标注的相关文档（ground truth）。

    来源：preset（预置查询模板，无 ground_truth，需用户标注）/ custom（用户自定义）
    """

    __tablename__ = "ai_eval_rag_queries"

    dataset_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("ai_eval_rag_datasets.id"),
        nullable=False, comment="所属数据集 ID"
    )
    # 查询文本（用户问题）
    query: Mapped[str] = mapped_column(
        Text, nullable=False, comment="查询文本"
    )
    # 查询类型：exact_match / semantic / synonym / cross_lingual /
    # fuzzy / multi_constraint
    query_type: Mapped[str] = mapped_column(
        String(30), default="semantic",
        comment="查询类型: exact_match/semantic/synonym/"
                "cross_lingual/fuzzy/multi_constraint",
    )
    # 难度：easy / medium / hard
    difficulty: Mapped[str] = mapped_column(
        String(20), default="medium",
        comment="难度: easy/medium/hard",
    )
    # 人工标注的相关文档 ID 列表（ground truth）
    # test.md：测试数据须人工标注且与算法隔离，防过拟合
    ground_truth_doc_ids: Mapped[list | None] = mapped_column(
        JSONB, nullable=True, comment="人工标注的相关文档 ID 列表"
    )
    # 期望答案（可选，供后续生成质量评测复用）
    expected_answer: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="期望答案（可选）"
    )
    # 来源：preset / custom
    source: Mapped[str] = mapped_column(
        String(20), default="custom", comment="来源: preset/custom"
    )

    # 关系
    dataset: Mapped[RagEvalDataset] = relationship(back_populates="queries")
    result: Mapped["RagEvalResult | None"] = relationship(
        back_populates="query", uselist=False, cascade="all, delete-orphan"
    )


class RagEvalResult(UUIDMixin, TimestampMixin, Base):
    """检索结果表 — 单条查询的检索结果与质量指标。

    指标（metrics JSONB）：
        recall_at_1/3/5、precision_at_1/3/5、mrr、ndcg_at_5、map、hit
    指标计算参考 test.md：基于标注 ground_truth 与检索排序结果计算 Recall@K / MAP。
    """

    __tablename__ = "ai_eval_rag_results"

    query_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("ai_eval_rag_queries.id"),
        nullable=False, comment="关联查询 ID"
    )
    dataset_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("ai_eval_rag_datasets.id"),
        nullable=False, comment="所属数据集 ID"
    )
    # 检索结果列表（按 rank 排序）：
    # [{doc_id, chunk_id, score, rank, source, title}, ...]
    retrieved: Mapped[list | None] = mapped_column(
        JSONB, nullable=True, comment="检索结果列表（按 rank 排序）"
    )
    # 质量指标 JSONB：{recall_at_1, recall_at_3, recall_at_5,
    #   precision_at_1, precision_at_3, precision_at_5,
    #   mrr, ndcg_at_5, map, hit}
    metrics: Mapped[dict | None] = mapped_column(
        JSONB, nullable=True, comment="检索质量指标"
    )
    # 检索返回的文档数（去重前）
    retrieved_count: Mapped[int] = mapped_column(
        Integer, default=0, comment="检索返回文档数"
    )
    # 响应耗时（毫秒）
    response_time_ms: Mapped[int] = mapped_column(
        Integer, default=0, comment="响应耗时（毫秒）"
    )
    # 错误信息（执行异常时）
    error_message: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="执行错误信息"
    )
    executed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="执行时间"
    )

    # 关系
    query: Mapped[RagEvalQuery] = relationship(back_populates="result")


# ======================================================================
# 文档解析评测 — 数据集 / 用例 / 结果
# 参考 test.md 第六部分：文本相似度 / 表格准确率 / 公式准确率 / 版面还原度。
# 评测对象：DoclingParser 解析输出与人工标注标准答案的对比。
# 标注方式：expected_text 为人工标注的标准文本（ground truth），
#           parsed_text 可直接提供或由 Docling 解析 document_id 对应文件得到。
# ======================================================================


class DocParseDataset(UUIDMixin, TimestampMixin, SoftDeleteMixin, Base):
    """文档解析评测数据集表 — 一次完整解析评测的顶层容器。

    生命周期：created → running → completed / failed

    聚合指标（metrics JSONB）含：
        text_similarity、cer、table_score、formula_score、layout_score、overall_score
    """

    __tablename__ = "ai_eval_doc_parse_datasets"

    name: Mapped[str] = mapped_column(
        String(255), nullable=False, comment="数据集名称"
    )
    description: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="数据集描述"
    )
    status: Mapped[str] = mapped_column(
        String(20), default="created",
        comment="状态: created/running/completed/failed",
    )
    total_cases: Mapped[int] = mapped_column(
        Integer, default=0, comment="用例总数"
    )
    # 聚合指标 JSONB
    metrics: Mapped[dict | None] = mapped_column(
        JSONB, nullable=True, comment="聚合解析质量指标"
    )
    duration_seconds: Mapped[int] = mapped_column(
        Integer, default=0, comment="执行耗时（秒）"
    )
    created_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False, comment="创建者 ID"
    )
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True, comment="租户 ID（多租户隔离）"
    )

    # 关系
    cases: Mapped[list["DocParseCase"]] = relationship(
        back_populates="dataset", cascade="all, delete-orphan"
    )


class DocParseCase(UUIDMixin, TimestampMixin, Base):
    """文档解析用例表 — 单条文档的解析评测定义。

    两种评测模式：
        1. 直接提供模式：expected_text（标注）+ parsed_text（待评测解析结果）
        2. Docling 端到端模式：expected_text（标注）+ document_id（下载文件后用 Docling 解析）
    """

    __tablename__ = "ai_eval_doc_parse_cases"

    dataset_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("ai_eval_doc_parse_datasets.id"),
        nullable=False, comment="所属数据集 ID"
    )
    title: Mapped[str] = mapped_column(
        String(500), nullable=False, comment="用例标题"
    )
    # 文档类型：pdf/docx/pptx/xlsx/html/image
    doc_type: Mapped[str] = mapped_column(
        String(20), default="pdf", comment="文档类型: pdf/docx/pptx/xlsx/html/image"
    )
    # 难度：easy/medium/hard
    difficulty: Mapped[str] = mapped_column(
        String(20), default="medium", comment="难度: easy/medium/hard"
    )
    # 人工标注的标准答案文本（ground truth）
    expected_text: Mapped[str] = mapped_column(
        Text, nullable=False, comment="人工标注的标准答案文本"
    )
    # 可选：关联文档 ID（Docling 端到端模式，下载该文档并用 Docling 解析）
    document_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True, comment="关联文档 ID（Docling 端到端模式）"
    )
    # 可选：直接提供待评测的解析结果文本（直接提供模式）
    parsed_text: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="待评测的解析结果文本（直接提供模式）"
    )
    # 来源：preset / custom
    source: Mapped[str] = mapped_column(
        String(20), default="custom", comment="来源: preset/custom"
    )

    # 关系
    dataset: Mapped[DocParseDataset] = relationship(back_populates="cases")
    result: Mapped["DocParseResult | None"] = relationship(
        back_populates="case", uselist=False, cascade="all, delete-orphan"
    )


class DocParseResult(UUIDMixin, TimestampMixin, Base):
    """文档解析结果表 — 单条用例的解析结果与质量指标。

    指标（metrics JSONB）：text_similarity、cer、token_similarity、
    table{...}、formula{...}、layout{...}、overall_score
    """

    __tablename__ = "ai_eval_doc_parse_results"

    case_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("ai_eval_doc_parse_cases.id"),
        nullable=False, comment="关联用例 ID"
    )
    dataset_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("ai_eval_doc_parse_datasets.id"),
        nullable=False, comment="所属数据集 ID"
    )
    # 实际用于评测的解析结果文本（直接提供或 Docling 解析得到）
    parsed_text: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="实际解析结果文本"
    )
    # 质量指标 JSONB
    metrics: Mapped[dict | None] = mapped_column(
        JSONB, nullable=True, comment="解析质量指标"
    )
    overall_score: Mapped[float] = mapped_column(
        Integer, default=0, comment="综合得分（0-100）"
    )
    # 解析耗时（毫秒）
    parse_time_ms: Mapped[int] = mapped_column(
        Integer, default=0, comment="解析耗时（毫秒）"
    )
    # 是否使用 Docling 端到端解析
    used_docling: Mapped[bool] = mapped_column(
        default=False, comment="是否使用 Docling 端到端解析"
    )
    error_message: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="执行错误信息"
    )
    executed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="执行时间"
    )

    # 关系
    case: Mapped[DocParseCase] = relationship(back_populates="result")


# ======================================================================
# AI Judge 自动评测 — 数据集 / 用例 / 结果
# 参考 test.md 第七部分：用 LLM 作为裁判，对模型输出做多维评分。
# 评分维度：准确性 / 完整性 / 相关性 / 表达清晰度 / 安全性（可配置）。
# ======================================================================


class JudgeDataset(UUIDMixin, TimestampMixin, SoftDeleteMixin, Base):
    """AI Judge 评测数据集表 — 一次完整 LLM 裁判评测的顶层容器。

    生命周期：created → running → completed / failed
    """

    __tablename__ = "ai_eval_judge_datasets"

    name: Mapped[str] = mapped_column(
        String(255), nullable=False, comment="数据集名称"
    )
    description: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="数据集描述"
    )
    # 裁判模型标识（如 claude-sonnet / qwen-max / gpt-4o）
    judge_model: Mapped[str] = mapped_column(
        String(100), default="default", comment="裁判模型标识"
    )
    # 评分维度列表 JSONB：["accuracy","completeness","relevance","clarity","safety"]
    dimensions: Mapped[list | None] = mapped_column(
        JSONB, nullable=True, comment="评分维度列表"
    )
    status: Mapped[str] = mapped_column(
        String(20), default="created",
        comment="状态: created/running/completed/failed",
    )
    total_cases: Mapped[int] = mapped_column(
        Integer, default=0, comment="用例总数"
    )
    # 聚合评分 JSONB：{dimension: avg_score, overall: avg}
    metrics: Mapped[dict | None] = mapped_column(
        JSONB, nullable=True, comment="聚合裁判评分"
    )
    duration_seconds: Mapped[int] = mapped_column(
        Integer, default=0, comment="执行耗时（秒）"
    )
    created_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False, comment="创建者 ID"
    )
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True, comment="租户 ID（多租户隔离）"
    )

    # 关系
    cases: Mapped[list["JudgeCase"]] = relationship(
        back_populates="dataset", cascade="all, delete-orphan"
    )


class JudgeCase(UUIDMixin, TimestampMixin, Base):
    """AI Judge 用例表 — 单条问答的裁判评测定义。

    包含：问题 + 参考答案 + 待评测的模型答案。
    """

    __tablename__ = "ai_eval_judge_cases"

    dataset_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("ai_eval_judge_datasets.id"),
        nullable=False, comment="所属数据集 ID"
    )
    # 问题/指令
    question: Mapped[str] = mapped_column(
        Text, nullable=False, comment="问题/指令"
    )
    # 参考答案（标准答案，供裁判对比）
    reference_answer: Mapped[str] = mapped_column(
        Text, nullable=False, comment="参考答案（标准答案）"
    )
    # 待评测的模型答案
    model_answer: Mapped[str] = mapped_column(
        Text, nullable=False, comment="待评测的模型答案"
    )
    # 场景类别：instruction/qa/reasoning/code/roleplay/safety
    category: Mapped[str] = mapped_column(
        String(30), default="qa",
        comment="场景类别: instruction/qa/reasoning/code/roleplay/safety",
    )
    # 来源：preset / custom
    source: Mapped[str] = mapped_column(
        String(20), default="custom", comment="来源: preset/custom"
    )

    # 关系
    dataset: Mapped[JudgeDataset] = relationship(back_populates="cases")
    result: Mapped["JudgeResult | None"] = relationship(
        back_populates="case", uselist=False, cascade="all, delete-orphan"
    )


class JudgeResult(UUIDMixin, TimestampMixin, Base):
    """AI Judge 结果表 — 单条用例的裁判评分。

    scores JSONB：{dimension: score(0-100)}，如 {"accuracy": 90, "clarity": 85}
    """

    __tablename__ = "ai_eval_judge_results"

    case_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("ai_eval_judge_cases.id"),
        nullable=False, comment="关联用例 ID"
    )
    dataset_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("ai_eval_judge_datasets.id"),
        nullable=False, comment="所属数据集 ID"
    )
    # 各维度评分 JSONB：{dimension: score}
    scores: Mapped[dict | None] = mapped_column(
        JSONB, nullable=True, comment="各维度评分（0-100）"
    )
    overall_score: Mapped[int] = mapped_column(
        Integer, default=0, comment="综合得分（0-100）"
    )
    # 裁判评语/理由
    reasoning: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="裁判评语/理由"
    )
    # 裁判原始响应（便于复核）
    raw_response: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="裁判原始响应"
    )
    judge_model: Mapped[str | None] = mapped_column(
        String(100), nullable=True, comment="实际使用的裁判模型"
    )
    response_time_ms: Mapped[int] = mapped_column(
        Integer, default=0, comment="响应耗时（毫秒）"
    )
    error_message: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="执行错误信息"
    )
    executed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="执行时间"
    )

    # 关系
    case: Mapped[JudgeCase] = relationship(back_populates="result")
