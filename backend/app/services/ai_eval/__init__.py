"""
AI 评测服务包 — Prompt Injection、RAG 检索、文档解析、AI Judge 评测。

遵循单一职责：每个子模块负责一类评测，互不依赖。
"""

from app.services.ai_eval.doc_parse_metrics import compute_parse_metrics
from app.services.ai_eval.doc_parse_service import DocParseService
from app.services.ai_eval.injection_test_service import InjectionTestService
from app.services.ai_eval.injection_vectors import (
    ATTACK_VECTORS,
    get_preset_cases,
)
from app.services.ai_eval.judge_service import (
    DEFAULT_DIMENSIONS,
    DIMENSION_NAMES,
    JudgeService,
    build_judge_prompt,
    parse_judge_response,
)
from app.services.ai_eval.rag_eval_queries import (
    PRESET_QUERIES,
    get_preset_queries,
)
from app.services.ai_eval.rag_eval_service import (
    RagEvalService,
    compute_retrieval_metrics,
)

__all__ = [
    "InjectionTestService",
    "ATTACK_VECTORS",
    "get_preset_cases",
    "RagEvalService",
    "compute_retrieval_metrics",
    "PRESET_QUERIES",
    "get_preset_queries",
    "DocParseService",
    "compute_parse_metrics",
    "JudgeService",
    "build_judge_prompt",
    "parse_judge_response",
    "DEFAULT_DIMENSIONS",
    "DIMENSION_NAMES",
]
