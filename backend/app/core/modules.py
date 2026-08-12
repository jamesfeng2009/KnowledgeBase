"""
模块注册表 — 定义所有可按租户开关的功能模块。

遵循开闭原则：新增模块只需在 MODULE_REGISTRY 列表中追加 ModuleDef 条目，
无需修改 require_module 依赖或 TenantService 业务逻辑。

模块分两类：
    1. 基础模块（is_basic=True）：所有套餐必含，不可关闭
    2. 可选模块（is_basic=False）：按租户套餐和购买情况开关

数据存储：模块列表保存在 Tenant.settings JSONB 字段的 enabled_modules 键中。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ModuleDef:
    """模块定义 — 不可变值对象。"""

    id: str  # 模块标识（对应 settings.enabled_modules 中的值）
    name: str  # 中文显示名称
    description: str  # 模块功能描述
    category: str  # 分类: basic / intelligence / integration / governance
    is_basic: bool = False  # 是否为基础模块（所有套餐必含，不可关闭）


# ------------------------------------------------------------------
# 模块注册表 — 新增模块只需在此追加条目
# ------------------------------------------------------------------

MODULE_REGISTRY: list[ModuleDef] = [
    # === 基础模块（所有套餐必含，不可关闭）===
    ModuleDef(
        id="knowledge_base",
        name="知识库",
        description="文档管理、知识库 CRUD、全文搜索",
        category="basic",
        is_basic=True,
    ),
    ModuleDef(
        id="audit_workflow",
        name="审核工作流",
        description="文档审核、发布流程、版本控制",
        category="basic",
        is_basic=True,
    ),
    ModuleDef(
        id="qa_community",
        name="问答社区",
        description="问答帖子、回答、评论",
        category="basic",
        is_basic=True,
    ),
    ModuleDef(
        id="external_sync",
        name="外部文档实时同步",
        description="飞书/Confluence/Notion/Obsidian 文档回源校验、凭证管理、Webhook 同步",
        category="basic",
        is_basic=True,
    ),
    # === 智能处理模块 ===
    ModuleDef(
        id="doc_intelligence",
        name="文档智能处理",
        description="AI 自动摘要、标签提取、自动分类、行动项识别",
        category="intelligence",
    ),
    ModuleDef(
        id="analytics_dashboard",
        name="知识健康度仪表盘",
        description="搜索热词、零点击分析、覆盖率、新鲜度、贡献排行",
        category="intelligence",
    ),
    ModuleDef(
        id="knowledge_graph",
        name="知识图谱",
        description="Neo4j 图谱可视化、关联推荐、批量建图、混合三元组提取",
        category="intelligence",
    ),
    # === 集成与发现模块 ===
    ModuleDef(
        id="expert_discovery",
        name="专家发现",
        description="多维度加权评分查找领域专家、贡献排行榜",
        category="integration",
    ),
    ModuleDef(
        id="knowledge_push",
        name="知识主动推送",
        description="个性化日报、文档变更通知、知识缺口预警",
        category="integration",
    ),
    ModuleDef(
        id="unified_search",
        name="跨系统统一搜索",
        description="OA/ERP/CRM/邮件连接器并行检索、权限联邦过滤",
        category="integration",
    ),
    ModuleDef(
        id="multimodal",
        name="多模态知识处理",
        description="图片智能解析、表格结构化、扫描件 OCR、白板拍照入库",
        category="integration",
    ),
    # === 智能测试平台 ===
    ModuleDef(
        id="testing_platform",
        name="智能测试平台",
        description="PRD/UI 稿自动需求拆分、AI 用例生成、用例评审、统一管理、AI 自动编排、知识回流层（4 类知识资产沉淀 + 冲突检测 + 复用注入）",
        category="intelligence",
    ),
    # === 知识推荐 ===
    ModuleDef(
        id="knowledge_recommendation",
        name="知识推荐",
        description="基于用户行为 + 内容语义 + 图谱关联的个性化知识推荐、相关阅读",
        category="integration",
    ),
]

# ------------------------------------------------------------------
# 便捷查询（模块集合，O(1) 查找）
# ------------------------------------------------------------------

MODULE_IDS: set[str] = {m.id for m in MODULE_REGISTRY}
BASIC_MODULE_IDS: set[str] = {m.id for m in MODULE_REGISTRY if m.is_basic}
OPTIONAL_MODULE_IDS: set[str] = MODULE_IDS - BASIC_MODULE_IDS
MODULE_MAP: dict[str, ModuleDef] = {m.id: m for m in MODULE_REGISTRY}

# ------------------------------------------------------------------
# 套餐默认模块 — 首次创建租户或 settings 无 enabled_modules 时使用
# ------------------------------------------------------------------

PLAN_DEFAULTS: dict[str, list[str]] = {
    "free": sorted(BASIC_MODULE_IDS),
    "pro": sorted(
        BASIC_MODULE_IDS
        | {
            "doc_intelligence",
            "analytics_dashboard",
            "knowledge_graph",
            "expert_discovery",
            "knowledge_push",
            "testing_platform",
        }
    ),
    "enterprise": sorted(MODULE_IDS),
}


def get_module_info(module_id: str) -> ModuleDef | None:
    """获取模块定义。"""
    return MODULE_MAP.get(module_id)


def is_valid_module(module_id: str) -> bool:
    """检查模块 ID 是否有效。"""
    return module_id in MODULE_IDS


def merge_with_basics(module_ids: list[str]) -> list[str]:
    """将模块列表与基础模块合并（基础模块永远包含）。"""
    return sorted(set(module_ids) | BASIC_MODULE_IDS)
