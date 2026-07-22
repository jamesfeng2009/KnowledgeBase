# P0 多租户数据隔离设计方案

> 状态：待 Review | 创建时间：2026-07-21 | 预估总工期：12-16 天

## 一、现状分析

### 1.1 已就绪部分（无需改动）

| 组件 | 状态 | 说明 |
|------|------|------|
| Tenant 模型 | ✅ 完整 | name/domain/plan/max_users/max_storage/settings(JSONB)/expired_at |
| tenant_id 列 | ✅ 26 个模型已预留 | 17 个无 FK 无索引，5 个有 FK 无索引，4 个有索引无 FK |
| 模块门控 | ✅ 完整 | TenantService.is_module_enabled / get_enabled_modules / toggle_module |
| require_module 依赖 | ✅ 已实现 | 但不传 tenant_id，取第一条活跃租户 |
| PLAN_DEFAULTS | ✅ 三档定义 | free（3 基础）/ pro（8 模块）/ enterprise（10 模块） |

### 1.2 缺失部分（本方案要解决的）

| 缺失项 | 风险等级 | 影响范围 |
|--------|---------|---------|
| **User 表无 tenant_id** | 🔴 严重 | 用户体系完全无租户归属，无法确定当前请求的租户 |
| **JWT payload 无 tenant_id** | 🔴 严重 | 每次请求无法从 token 获取租户上下文 |
| **无 TenantContextMiddleware** | 🔴 严重 | 中间件层没有租户上下文注入机制 |
| **Repository 层无过滤** | 🔴 严重 | 13 个 repo 中仅 1 个(billing)过滤 tenant_id，其余 12 个完全未过滤 |
| **Service 层无过滤** | 🔴 严重 | knowledge/search/qa/graph 4 个核心服务完全无 tenant_id 引用 |
| **BaseRepository 无自动注入** | 🟡 中等 | 基类没有封装 tenant_id 自动过滤，需逐 repo 改造 |
| **索引缺失** | 🟡 中等 | 17 个核心业务表的 tenant_id 无索引，补过滤后将全表扫描 |
| **FK 约束缺失** | 🟡 中等 | 17 个模型的 tenant_id 为裸字段无 FK，无法保证引用完整性 |
| **QA 模型无 tenant_id** | 🟡 中等 | QaQuestion/QaAnswer 完全没有 tenant_id 字段 |
| **RLS 兜底缺失** | 🟢 低 | 应用层遗漏过滤时无数据库兜底 |

### 1.3 当前数据流（无隔离）

```
用户请求 → JWT(仅 sub+role) → get_current_user → 查 User(无 tenant_id)
    → Repository 查询(无 tenant_id 过滤) → 返回全局数据
```

问题：任何用户都能看到所有租户的文档、对话、搜索结果。

## 二、设计目标

### 2.1 核心目标

1. **租户隔离**：任何查询只能返回当前租户的数据，杜绝跨租户数据泄漏
2. **向后兼容**：tenant_id=NULL 的历史数据不破坏，单租户场景自动兜底
3. **纵深防御**：应用层过滤（Repository 自动注入）+ 数据库兜底（RLS 策略）双层保障
4. **最小侵入**：通过 BaseRepository 封装自动注入，减少逐 repo 改造量
5. **性能优先**：先加索引再加过滤，避免全表扫描

### 2.2 隔离边界

| 层级 | 隔离机制 | 覆盖范围 |
|------|---------|---------|
| **L1 中间件层** | TenantContextMiddleware 从 JWT 提取 tenant_id 注入 request.state | 所有 HTTP 请求 |
| **L2 依赖注入层** | get_current_user 返回带 tenant_id 的 User，require_module 传入 tenant_id | 所有需认证的 API |
| **L3 Repository 层** | BaseRepository 自动注入 tenant_id 过滤条件 | 所有数据库查询 |
| **L4 数据库层** | PostgreSQL RLS 策略兜底 | 应用层遗漏时数据库兜底 |

## 三、架构设计

### 3.1 目标数据流（4 层隔离）

```
用户请求
    ↓
┌─ L1: TenantContextMiddleware ──────────────────────┐
│  从 JWT 解析 tenant_id → request.state.tenant_id   │
│  无 tenant_id 时取第一条活跃租户（单租户兜底）        │
└─────────────────────────────────────────────────────┘
    ↓
┌─ L2: get_current_user + require_module ────────────┐
│  User 对象携带 tenant_id                            │
│  require_module(module, tenant_id) 正确门控          │
└─────────────────────────────────────────────────────┘
    ↓
┌─ L3: BaseRepository 自动注入 ───────────────────────┐
│  所有查询自动追加 WHERE tenant_id = :current_tenant  │
│  tenant_id IS NULL 的历史数据自动包含（兼容期）       │
└─────────────────────────────────────────────────────┘
    ↓
┌─ L4: PostgreSQL RLS 策略（兜底）────────────────────┐
│  SET LOCAL app.tenant_id = xxx                      │
│  CREATE POLICY ... USING (tenant_id = current_setting('app.tenant_id')::uuid) │
│  应用层遗漏过滤时数据库兜底                           │
└─────────────────────────────────────────────────────┘
```

### 3.2 兼容期策略

历史数据 tenant_id=NULL，直接过滤 `tenant_id = xxx` 会丢失数据。采用分阶段策略：

| 阶段 | 过滤条件 | 说明 |
|------|---------|------|
| **兼容期（P3 上线后 3 个月）** | `WHERE tenant_id = :tid OR tenant_id IS NULL` | NULL 数据所有租户可见，逐步迁移 |
| **迁移完成后** | `WHERE tenant_id = :tid` | 严格隔离，NULL 数据需已迁移 |

提供 CLI 命令 `python cli.py migrate-tenant-data --tenant-id xxx` 批量回填 NULL 数据。

### 3.3 单租户兜底

私有部署（DEPLOY_MODE=private）单租户场景，tenant_id=NULL 不过滤也不影响正确性：

```python
# BaseRepository._apply_tenant_filter()
def _apply_tenant_filter(self, stmt, tenant_id: UUID | None):
    if tenant_id is None:
        return stmt  # 单租户场景，不过滤
    return stmt.where(
        or_(
            self.model.tenant_id == tenant_id,
            self.model.tenant_id.is_(None),  # 兼容期
        )
    )
```

## 四、详细 Task 分解

### P1：基础设施层 — User 关联租户 + JWT 改造（2-3 天）

> **目标**：让系统"知道"当前请求属于哪个租户

#### Task 1.1：User 模型加 tenant_id（0.5 天）

**文件**：`app/models/user.py`、`alembic/versions/xxx_add_user_tenant_id.py`

**改动**：
- User 模型新增 `tenant_id: Mapped[uuid.UUID | None]` 字段
- nullable=True（向后兼容历史用户）
- FK → tenants.id
- 索引 `ix_users_tenant_id`

**Alembic 迁移**：
```python
def upgrade():
    op.add_column('users', sa.Column('tenant_id', postgresql.UUID(as_uuid=True), nullable=True))
    op.create_foreign_key('fk_users_tenant_id', 'users', 'tenants', ['tenant_id'], ['id'])
    op.create_index('ix_users_tenant_id', 'users', ['tenant_id'])
```

#### Task 1.2：JWT payload 加 tenant_id（0.5 天）

**文件**：`app/utils/crypto.py`、`app/services/auth_service.py`

**改动**：
- `create_access_token` 不改签名，调用方在 data 中传入 tenant_id
- `AuthService.login` 查用户时关联 tenant，JWT payload 写入 `{"sub": ..., "role": ..., "tenant_id": ...}`
- `AuthService.register` 可选传入 tenant_id（注册时关联租户）
- `AuthService.get_current_user` 解析 JWT 后返回带 tenant_id 的 User

**关键代码**：
```python
# auth_service.py - login
user = await self.user_repo.get_by_email(email)
# ...校验密码...
token = create_access_token({
    "sub": str(user.id),
    "role": user.role,
    "tenant_id": str(user.tenant_id) if user.tenant_id else None,
})
```

#### Task 1.3：TenantContextMiddleware（0.5 天）

**文件**：`app/middleware.py`

**改动**：
- 新增 `TenantContextMiddleware`，从 JWT 解析 tenant_id 写入 `request.state.tenant_id`
- 无 JWT 或无 tenant_id 时取第一条活跃租户（单租户兜底）
- 注册到 `setup_middleware` 中，在限流中间件之后执行

**关键代码**：
```python
@app.middleware("http")
async def tenant_context_middleware(request: Request, call_next):
    # 白名单路径跳过
    if request.url.path.startswith(_EXEMPT_PATHS):
        return await call_next(request)

    # 从 Authorization header 解析 JWT
    token = request.headers.get("authorization", "").replace("Bearer ", "")
    tenant_id = None
    if token:
        try:
            payload = decode_access_token(token)
            tid_str = payload.get("tenant_id")
            if tid_str:
                tenant_id = UUID(tid_str)
        except Exception:
            pass

    # 单租户兜底：无 tenant_id 时不过滤
    request.state.tenant_id = tenant_id
    return await call_next(request)
```

#### Task 1.4：修复 require_module 传入 tenant_id（0.5 天）

**文件**：`app/deps.py`

**改动**：
- `require_module` 中的 `_check_module` 从 `request.state.tenant_id` 获取租户 ID
- 传入 `TenantService.is_module_enabled(module_name, tenant_id)`

**关键代码**：
```python
def require_module(module_name: str):
    async def _check_module(
        request: Request,
        user: User = Depends(get_current_user),
        db: AsyncSession = Depends(get_db_session),
    ) -> User:
        from app.services.tenant_service import TenantService
        tenant_id = getattr(request.state, "tenant_id", None)
        service = TenantService(db)
        if not await service.is_module_enabled(module_name, tenant_id):
            raise HTTPException(403, detail=f"当前套餐未包含「{module_name}」功能")
        return user
    return _check_module
```

#### Task 1.5：get_db_session 注入 tenant_id 到 session info（0.5 天）

**文件**：`app/database.py`

**改动**：
- `get_db_session` 依赖注入时，从 request.state 获取 tenant_id
- 执行 `SET LOCAL app.tenant_id = xxx`（供 RLS 策略使用，P4 启用）
- 需要修改 `get_db_session` 签名，增加 `request: Request` 参数

**关键代码**：
```python
async def get_db_session(request: Request) -> AsyncSession:
    async with async_session_maker() as session:
        tenant_id = getattr(request.state, "tenant_id", None)
        if tenant_id:
            await session.execute(
                text("SET LOCAL app.tenant_id = :tid"),
                {"tid": str(tenant_id)},
            )
        yield session
```

#### Task 1.6：测试（0.5 天）

**文件**：`tests/test_tenant_context.py`（新增）

**测试用例**：
- JWT payload 包含 tenant_id
- TenantContextMiddleware 正确解析 tenant_id
- 无 JWT 时 request.state.tenant_id = None
- require_module 传入正确的 tenant_id
- User 模型 tenant_id 字段创建/查询

---

### P2：数据层基础 — BaseRepository 封装 + 索引 + FK（2-3 天）

> **目标**：为 Repository 层自动过滤做好基础设施

#### Task 2.1：BaseRepository 封装 tenant_id 自动过滤（1 天）

**文件**：`app/repositories/base.py`

**改动**：
- 新增 `_apply_tenant_filter(stmt, tenant_id)` 方法
- `get_all`、`count`、`get_by_id` 等查询方法自动调用 `_apply_tenant_filter`
- `create` 方法自动写入 tenant_id
- 子类可覆盖 `_apply_tenant_filter` 实现特殊逻辑

**关键代码**：
```python
class BaseRepository(Generic[ModelT]):
    def __init__(self, db: AsyncSession, tenant_id: UUID | None = None):
        self._db = db
        self._tenant_id = tenant_id

    def _apply_tenant_filter(self, stmt):
        """自动注入 tenant_id 过滤条件。"""
        if self._tenant_id is None:
            return stmt  # 单租户兜底
        if not hasattr(self.model, 'tenant_id'):
            return stmt  # 模型无 tenant_id 字段
        return stmt.where(
            or_(
                self.model.tenant_id == self._tenant_id,
                self.model.tenant_id.is_(None),  # 兼容期
            )
        )

    async def get_all(self, offset=0, limit=20):
        stmt = select(self.model).where(self.model.deleted_at.is_(None))
        stmt = self._apply_tenant_filter(stmt)
        stmt = stmt.offset(offset).limit(limit)
        result = await self._db.execute(stmt)
        return result.scalars().all()

    async def create(self, **kwargs):
        if self._tenant_id and hasattr(self.model, 'tenant_id'):
            kwargs.setdefault('tenant_id', self._tenant_id)
        obj = self.model(**kwargs)
        self._db.add(obj)
        await self._db.flush()
        return obj
```

#### Task 2.2：Repository 构造函数注入 tenant_id（0.5 天）

**文件**：`app/repositories/*.py`（13 个文件）

**改动**：
- 所有 Repository 的 `__init__` 增加 `tenant_id` 参数
- 传递给 `BaseRepository.__init__`

#### Task 2.3：Service 层从 request.state 获取 tenant_id 并传给 Repository（0.5 天）

**文件**：`app/services/*.py`（核心 5 个服务）

**改动**：
- `knowledge_service.py`、`search_service.py`、`qa_service.py`、`graph_service.py`、`doc_intelligence_service.py`
- Service 构造函数增加 `tenant_id` 参数
- 创建 Repository 时传入 tenant_id

**关键代码**：
```python
class KnowledgeService:
    def __init__(self, db: AsyncSession, tenant_id: UUID | None = None):
        self._db = db
        self._tenant_id = tenant_id
        self._kb_repo = KnowledgeRepository(db, tenant_id=tenant_id)
        self._doc_repo = DocumentRepository(db, tenant_id=tenant_id)
```

#### Task 2.4：API 路由层注入 tenant_id 到 Service（0.5 天）

**文件**：`app/api/v1/*.py`（约 20 个路由文件）

**改动**：
- 路由函数从 `request.state.tenant_id` 获取租户 ID
- 创建 Service 时传入 tenant_id

**关键代码**：
```python
@router.get("/knowledge-bases")
async def list_kbs(
    request: Request,
    db: AsyncSession = Depends(get_db_session),
    user: User = Depends(get_current_active_user),
):
    tenant_id = getattr(request.state, "tenant_id", None)
    service = KnowledgeService(db, tenant_id=tenant_id)
    kbs = await service.list_kbs(user_id=user.id)
    return ApiResponse(data=kbs)
```

#### Task 2.5：Alembic 迁移 — 补齐索引 + FK（0.5 天）

**文件**：`alembic/versions/xxx_add_tenant_id_indexes_fk.py`

**改动**：
- 17 个裸字段 tenant_id 补 FK 约束 → tenants.id
- 22 个无索引的 tenant_id 补 B-tree 索引
- QA 模型(QaQuestion/QaAnswer) 补 tenant_id 列

**迁移脚本**：
```python
def upgrade():
    # 补 FK
    for table in TABLES_NEED_FK:
        op.create_foreign_key(
            f'fk_{table}_tenant_id', table, 'tenants',
            ['tenant_id'], ['id'],
        )
    # 补索引
    for table in TABLES_NEED_INDEX:
        op.create_index(f'ix_{table}_tenant_id', table, ['tenant_id'])
    # QA 模型补 tenant_id
    op.add_column('qa_questions', sa.Column('tenant_id', postgresql.UUID(as_uuid=True), nullable=True))
    op.add_column('qa_answers', sa.Column('tenant_id', postgresql.UUID(as_uuid=True), nullable=True))
    op.create_index('ix_qa_questions_tenant_id', 'qa_questions', ['tenant_id'])
    op.create_index('ix_qa_answers_tenant_id', 'qa_answers', ['tenant_id'])
```

#### Task 2.6：测试（0.5 天）

**文件**：`tests/test_tenant_isolation.py`（新增）

**测试用例**：
- BaseRepository._apply_tenant_filter 正确过滤
- tenant_id=None 时不过滤（单租户兜底）
- tenant_id=None 的历史数据在兼容期可见
- create 方法自动写入 tenant_id
- QA 模型 tenant_id 字段

---

### P3：查询过滤全覆盖 — 12 个 Repository + 5 个核心 Service（5-7 天）

> **目标**：所有业务查询都带 tenant_id 过滤

#### Task 3.1：knowledge_repository.py 改造（1 天）

**文件**：`app/repositories/knowledge_repository.py`

**改动的方法**：
- `get_by_owner` → 加 tenant_id 过滤
- `get_accessible_kbs` → 加 tenant_id 过滤
- `get_by_kb` → 加 tenant_id 过滤
- `get_by_classification` → 加 tenant_id 过滤
- `search_text` → 加 tenant_id 过滤
- 所有自定义查询方法

#### Task 3.2：qa_repository.py 改造 + QaQuestion/QaAnswer 模型加 tenant_id（1 天）

**文件**：`app/repositories/qa_repository.py`、`app/models/qa.py`

**改动**：
- QaQuestion 模型加 tenant_id 字段
- QaAnswer 模型加 tenant_id 字段
- 所有查询方法加 tenant_id 过滤

#### Task 3.3：conversation_repository.py 改造（0.5 天）

**文件**：`app/repositories/conversation_repository.py`

**改动的方法**：get_by_user、get_with_messages、get_by_conversation

#### Task 3.4：comment_repository.py 改造（0.5 天）

**文件**：`app/repositories/comment_repository.py`

**改动的方法**：get_by_doc、get_replies

#### Task 3.5：audit_repository.py 改造（0.5 天）

**文件**：`app/repositories/audit_repository.py`

**改动的方法**：get_by_status、get_by_resource、get_by_submitter、get_by_reviewer

#### Task 3.6：其余 Repository 改造（1 天）

**文件**：
- `apikey_repository.py` — get_by_prefix、list_all、update_last_used、deactivate
- `feedback_repository.py` — get_by_status、get_by_user
- `gap_repository.py` — get_by_topic、get_all、update_status、increment_search_count
- `report_repository.py` — get_usage_stats、get_query_logs、get_cost_stats、get_knowledge_stats
- `user_repository.py` — 特殊处理（用户按 tenant_id 过滤，但 get_by_email 需要跨租户查唯一邮箱）

#### Task 3.7：search_service.py 改造（1 天）

**文件**：`app/services/search_service.py`

**改动**：
- search 方法加 tenant_id 过滤
- _db_fulltext_search 加 tenant_id 过滤
- _get_accessible_kb_ids 加 tenant_id 过滤
- unified_search 加 tenant_id 过滤
- Milvus/OpenSearch 查询加 tenant_id 过滤条件

#### Task 3.8：graph_service.py 改造（0.5 天）

**文件**：`app/services/graph_service.py`

**改动**：
- Neo4j 查询加 tenant_id 过滤（Cypher WHERE 子句）
- get_related_recommendations 加 tenant_id
- batch_import_document 写入 tenant_id
- PG 降级查询加 tenant_id

#### Task 3.9：doc_intelligence_service.py + 其余 Service 改造（0.5 天）

**文件**：
- `doc_intelligence_service.py` — auto_summarize/auto_tag/auto_classify 加 tenant_id
- `analytics_service.py` — 所有统计查询加 tenant_id
- `notification_service.py` — 推送查询加 tenant_id
- `expert_service.py` — 专家发现加 tenant_id
- `chat_service.py` — 已有 tenant_id 参数，补上实际过滤逻辑
- `approval_service.py` — 已有 tenant_id 参数，补上实际过滤逻辑

#### Task 3.10：Celery 任务注入 tenant_id（0.5 天）

**文件**：`tasks/document_tasks.py`、`tasks/intelligence_tasks.py`、`tasks/multimodal_tasks.py` 等

**改动**：
- Celery 任务签名增加 tenant_id 参数
- 任务内部创建 Repository/Service 时传入 tenant_id
- 调用方（API 路由）提交任务时传入 tenant_id

**关键代码**：
```python
# API 路由提交任务
@router.post("/documents/upload")
async def upload_document(request: Request, ...):
    tenant_id = getattr(request.state, "tenant_id", None)
    process_document.delay(doc_id, tenant_id=str(tenant_id) if tenant_id else None)

# Celery 任务
@celery_app.task
def process_document(doc_id: str, tenant_id: str | None = None):
    tid = UUID(tenant_id) if tenant_id else None
    repo = DocumentRepository(db, tenant_id=tid)
    # ...
```

#### Task 3.11：全面测试（1 天）

**文件**：`tests/test_tenant_isolation_full.py`（新增）

**测试用例**：
- 租户 A 的用户看不到租户 B 的文档
- 租户 A 的用户搜索结果不含租户 B 的文档
- 租户 A 的用户对话历史不含租户 B 的对话
- 租户 A 的 Celery 任务只处理租户 A 的文档
- Milvus/OpenSearch 查询带 tenant_id 过滤
- Neo4j 查询带 tenant_id 过滤

---

### P4：RLS 兜底 + 数据迁移（2-3 天，可选）

> **目标**：数据库层兜底 + 历史数据迁移

#### Task 4.1：PostgreSQL RLS 策略（1.5 天）

**文件**：`alembic/versions/xxx_add_rls_policies.py`

**改动**：
- 为 24+ 个含 tenant_id 的表创建 RLS policy
- 每次会话 `SET LOCAL app.tenant_id = xxx`
- policy 自动过滤 `tenant_id = current_setting('app.tenant_id')::uuid`

**迁移脚本**：
```python
TENANT_TABLES = [
    'knowledge_bases', 'documents', 'conversations', 'messages',
    'qa_questions', 'qa_answers', 'document_comments', 'notifications',
    'search_logs', 'feedbacks', 'document_actions', 'tool_approvals',
    'user_model_preferences', 'knowledge_assets', 'compounding_tasks',
    'knowledge_conflicts', 'memory_facts', 'graphiti_entities',
    'graphiti_events', 'agent_configs', 'api_keys', 'knowledge_gaps',
    'test_projects', 'test_requirements', 'test_cases', 'test_plans',
]

def upgrade():
    for table in TENANT_TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"""
            CREATE POLICY tenant_isolation ON {table}
            USING (
                tenant_id = current_setting('app.tenant_id', true)::uuid
                OR tenant_id IS NULL
                OR current_setting('app.tenant_id', true) = ''
            )
        """)
```

#### Task 4.2：历史数据迁移 CLI（0.5 天）

**文件**：`cli.py`（新增命令）

**改动**：
- `python cli.py migrate-tenant-data --tenant-id xxx --table documents`
- 将指定表的 tenant_id=NULL 的数据回填为指定租户 ID
- 支持 `--dry-run` 预览

#### Task 4.3：关闭兼容期（0.5 天）

**改动**：
- BaseRepository._apply_tenant_filter 移除 `tenant_id.is_(None)` 兼容条件
- RLS policy 移除 `tenant_id IS NULL` 条件
- 仅在所有历史数据迁移完成后执行

#### Task 4.4：RLS 测试（0.5 天）

**测试用例**：
- RLS 启用后，未 SET app.tenant_id 时查不到数据
- SET app.tenant_id = A 后只能查到租户 A 的数据
- 应用层遗漏过滤时 RLS 兜底

---

## 五、影响范围统计

### 5.1 文件改动清单

| 层级 | 文件数 | 改动类型 |
|------|--------|---------|
| 模型层 | 3 | User/QaQuestion/QaAnswer 加 tenant_id |
| 迁移 | 3 | user_tenant_id + indexes_fk + rls_policies |
| 中间件 | 1 | middleware.py 新增 TenantContextMiddleware |
| 依赖注入 | 2 | deps.py + database.py |
| Repository | 13 | 全部改造构造函数 + 查询方法 |
| Service | 8 | 5 核心 + 3 预留补实 |
| API 路由 | ~20 | 注入 tenant_id 到 Service |
| Celery 任务 | 6 | 签名增加 tenant_id |
| 工具 | 2 | crypto.py + cli.py |
| 测试 | 3 | 新增 3 个测试文件 |
| **合计** | **~61** | |

### 5.2 数据库改动

| 改动项 | 数量 |
|--------|------|
| 新增列 | 3（users.tenant_id + qa_questions.tenant_id + qa_answers.tenant_id） |
| 新增 FK | 19（17 裸字段 + 2 QA 表） |
| 新增索引 | 24 |
| RLS 策略 | 26 |

## 六、风险与回退

### 6.1 风险评估

| 风险 | 概率 | 影响 | 缓解措施 |
|------|------|------|---------|
| Repository 改造遗漏 | 中 | 数据泄漏 | RLS 兜底 + 集成测试覆盖 |
| 性能下降（索引缺失） | 低 | 查询变慢 | P2 先加索引再改查询 |
| 兼容期 NULL 数据问题 | 中 | 数据泄漏 | 3 个月后关闭兼容期 |
| Celery 任务遗漏 tenant_id | 中 | 跨租户处理 | 任务签名强制传 tenant_id |
| User.email 跨租户唯一性 | 低 | 注册冲突 | 邮箱全局唯一 + 租户内显示名 |

### 6.2 回退方案

每个阶段独立可回退：
- P1 回退：移除 TenantContextMiddleware，JWT 不含 tenant_id
- P2 回退：BaseRepository._apply_tenant_filter 返回原 stmt
- P3 回退：各 Repository 移除 tenant_id 过滤
- P4 回退：`ALTER TABLE ... DISABLE ROW LEVEL SECURITY`

## 七、执行顺序

```
P1（基础设施层）→ P2（数据层基础）→ P3（查询过滤全覆盖）→ P4（RLS 兜底）
    2-3 天           2-3 天              5-7 天              2-3 天
```

**P1 和 P2 可部分并行**：P2.1（BaseRepository 封装）不依赖 P1，可提前开始。

**P3 必须在 P2 完成后**：Repository 构造函数改造依赖 BaseRepository 封装。

**P4 必须在 P3 完成后**：RLS 是兜底，应用层过滤必须先到位。
