"""
Celery 任务统一导出 — 导入所有任务模块，触发任务注册。

遵循单一职责：本文件仅做导出与注册触发，不包含业务逻辑。

任务模块：
- document_tasks: 文档处理流水线（解析 → 分块 → 向量化 → 索引）
- index_tasks: 索引构建与重建（OpenSearch 全文 + Milvus 向量）
- scheduled_tasks: 定时运维任务（缺口检测、过期预警、清理、报告）
"""

from tasks.document_tasks import batch_process_documents, process_document
from tasks.index_tasks import build_search_index, build_vector_index, rebuild_kb_index
from tasks.scheduled_tasks import (
    check_expiration,
    cleanup_expired_facts,
    detect_knowledge_gaps,
    generate_quality_report,
)

__all__ = [
    # 文档处理任务
    "process_document",
    "batch_process_documents",
    # 索引构建任务
    "build_search_index",
    "build_vector_index",
    "rebuild_kb_index",
    # 定时任务
    "detect_knowledge_gaps",
    "check_expiration",
    "cleanup_expired_facts",
    "generate_quality_report",
]
