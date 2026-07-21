"""
知识回流服务包 — 单一职责：知识回流层全部业务服务。

核心流程（5 步）：
    执行结果收集 → AI 知识提取 → 知识资产沉淀 → 冲突检测 → 复用注入

4 类知识资产：
    defect_experience     — 缺陷经验文档（Document + AI 摘要）
    regression_sop        — 回归 SOP（Document + TestPlan 关联）
    graph_association     — 知识图谱关联（Neo4j: requirement→case→defect→fix）
    verification_baseline — 验证基线时序（Graphiti: 旧基线 → historical_reference）

使用方式::

    from app.services.knowledge_compounding import KnowledgeCompoundingService
    from app.llm.factory import get_llm_provider

    async def extract_endpoint(db: AsyncSession = Depends(get_db_session)):
        llm = get_llm_provider()
        service = KnowledgeCompoundingService(llm, db)
        result = await service.extract_knowledge(execution_id)
"""

from app.services.knowledge_compounding.compounding_service import (
    KnowledgeCompoundingService,
)

__all__ = [
    "KnowledgeCompoundingService",
]
