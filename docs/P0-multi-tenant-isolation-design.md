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

---

## 八、RLS 的真实边界（实测结论）

> 本节是事后核验结论，回答一个关键问题：**数据库层 RLS 到底在不在生效？**
> 结论可能和直觉相反，部署前请务必读完。

### 8.1 组件是齐的

| 环节 | 现状 |
|------|------|
| 表级开关 | 36 张含 `tenant_id` 的表均为 `RELROWSECURITY = true` |
| 表级强化 | 上述表同时为 `RELFORCEROWSECURITY = true`（表 owner 也不能绕过） |
| 策略定义 | `current_setting('app.tenant_id', true) IS NULL OR tenant_id::text = current_setting('app.tenant_id', true)` |
| 运行时注入 | `app/database.py::get_db_session` 在 `request.state.tenant_id` 存在时执行 `SET LOCAL app.tenant_id = '<uuid>'`（先做 UUID 强校验再拼接） |
| 旁路会话 | `chat_service.py`、`memory_manager.py` 在独立会话中补设同一 GUC |

看起来是完整的。**但缺了最关键的一环。**

### 8.2 缺的一环：应用连的是超级用户

`DATABASE_URL` 里的连接角色（默认 `ekb`）在 PostgreSQL 中是 **superuser**：

```
select current_user, rolsuper from pg_roles where rolname = current_user;
-- ekb | t
```

而 PostgreSQL 对 superuser **无条件绕过 RLS** —— `FORCE ROW LEVEL SECURITY` 只覆盖表 owner，
**不覆盖 superuser**。实测对比（同一张开了 FORCE RLS 的表、同一个 `app.tenant_id`）：

| 查询角色 | 设置 tenant_id = 租户 A 后可见行数 | 说明 |
|---------|-------------------------------|------|
| `ekb`（superuser） | **2 行**（A + B 全可见） | RLS **未生效** |
| 普通角色（仅 GRANT SELECT） | **1 行**（只有 A） | RLS 生效 |

复现脚本（在任意库执行，末尾 ROLLBACK 不落盘）：

```sql
BEGIN;
CREATE TABLE public._rls_probe (id int primary key, tenant_id uuid);
ALTER TABLE public._rls_probe ENABLE ROW LEVEL SECURITY;
ALTER TABLE public._rls_probe FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON public._rls_probe
  USING (current_setting('app.tenant_id', true) IS NULL
         OR tenant_id::text = current_setting('app.tenant_id', true));
INSERT INTO public._rls_probe VALUES
  (1, '11111111-1111-1111-1111-111111111111'),
  (2, '22222222-2222-2222-2222-222222222222');
CREATE ROLE _rls_probe_role NOLOGIN;
GRANT SELECT ON public._rls_probe TO _rls_probe_role;
SET LOCAL app.tenant_id = '11111111-1111-1111-1111-111111111111';
SELECT count(*) FROM public._rls_probe;              -- superuser：2（绕过）
SET LOCAL ROLE _rls_probe_role;
SELECT count(*) FROM public._rls_probe;              -- 普通角色：1（生效）
ROLLBACK;
```

### 8.3 所以，隔离的真实边界是

1. **实际生效的只有应用层 `WHERE tenant_id = ...`**（`BaseRepository._apply_tenant_filter` +
   各 Repository 显式过滤）。172 个租户隔离测试验证的也是这一层。
2. **RLS 目前是零防护**，不是「最后一道防线」—— 因为连接角色是 superuser。
3. 另一处设计上的 fail-open：策略第一个分支 `current_setting(...) IS NULL` 意味着
   **GUC 未设置时全表可见**。任何绕过 `TenantContextMiddleware` 的入口
   （单租户兜底模式、脚本、Celery 任务漏传 tenant_id）都能读全租户数据。

### 8.4 要真正让 RLS 生效，需要三件事

1. **拆角色**：迁移/DDL 用 superuser（`ekb`），运行时装机用非 superuser 角色
   （如 `ekb_app`），只给 `SELECT/INSERT/UPDATE/DELETE` + 序列权限：
   ```sql
   CREATE ROLE ekb_app LOGIN PASSWORD '...' NOSUPERUSER NOBYPASSRLS;
   GRANT USAGE ON SCHEMA public TO ekb_app;
   GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO ekb_app;
   GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO ekb_app;
   ```
   然后运行时 `DATABASE_URL` 指向 `ekb_app`，迁移用独立 `MIGRATION_DATABASE_URL`（`ekb`）。
2. **关掉 fail-open**：把策略改成 GUC 缺失时**拒绝**（`tenant_id::text = current_setting('app.tenant_id', true)`），
   仅对确需跨租户的系统任务显式 `SET LOCAL app.tenant_id = ''` 之类的白名单通道开口子。
   这一步会打破「单租户兜底」现状，必须与业务确认后再做。
3. **防 GUC 泄漏**：`SET LOCAL` 随事务结束失效，前提是**一个请求一个事务**。
   若将来把 `NullPool` 换成连接池复用，必须确保每个事务开头都重设 GUC，
   否则残留值会串租户。

> 在完成第 1 步之前，**不要把 RLS 计入安全评估**。多租户隔离的正确性只能按
> 「应用层过滤是否完备」来验收。

---

## 九、第 1 步已落地：角色拆分与策略补全（2026-11-10）

上一节的三步整改里，**第 1 步（拆角色）已经实现并带测试**；第 2 步（关 fail-open）
刻意没动，需要业务确认；第 3 步（防 GUC 泄漏）是将来换连接池时的事前约束。

### 9.1 做了什么

| 项 | 内容 |
|----|------|
| 运行角色供给 | `backend/scripts/provision_app_role.py`（幂等）：建 `ekb_app`，`NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE`，给 `CONNECT` / `USAGE on schema public` / 表级 `SELECT,INSERT,UPDATE,DELETE` / 序列 `USAGE,SELECT`；**不给 `CREATE on schema`**（建表必须走迁移） |
| 未来表自动授权 | `ALTER DEFAULT PRIVILEGES FOR ROLE <每个真实表 owner>`，避免「迁移后运行时突然 permission denied」 |
| 连接分离 | `MIGRATION_DATABASE_URL`（owner，DDL/迁移）+ `DATABASE_URL`（运行角色）。`alembic/env.py`、`app/utils/migration.py`（`AUTO_MIGRATE` 启动路径）都改为优先读前者，未配置时回退后者 |
| 策略补全 | 迁移 `d7e8f9a0b1c2` 补齐 12 张漏掉的表：`document_chunks`、`knowledge_candidates`、`kb_folders`、`tickets`、`wiki_pages`、`research_jobs`、`constraint_rules`、`constraint_audit_records`、`ai_eval_*_datasets`×4 |

补全前后对比（`ekb` 库实测）：

```
补全前：48 张带 tenant_id 的表，36 张有 RLS，12 张零防护
补全后：48 张，48 张 ENABLE + FORCE，缺口 0
```

> 最容易漏的是 `document_chunks` —— 它存的是切片正文，正是 RAG 召回的原文。
> 只给 `documents` 开 RLS 而漏掉 chunks，等于隔离只做了目录没做内容。

### 9.2 部署步骤（三行）

```bash
cd backend
# 1) 建运行角色（用 owner 连接；口令建议 openssl rand -base64 24）
.venv/bin/python scripts/provision_app_role.py --password "$(openssl rand -base64 24)"

# 2) 把输出的 DATABASE_URL 写进运行环境；owner 连接留给 MIGRATION_DATABASE_URL
#    DATABASE_URL=postgresql+asyncpg://ekb_app:<pwd>@<host>:<port>/ekb
#    MIGRATION_DATABASE_URL=postgresql+asyncpg://ekb:<pwd>@<host>:<port>/ekb

# 3) 复核
.venv/bin/python scripts/dev_env_check.py   # 会打印「超级用户: 否」
```

`AUTO_MIGRATE=true` 时启动阶段用的是 `MIGRATION_DATABASE_URL`，所以运行角色没有
DDL 权限也能正常自动迁移。

### 9.3 守住它的测试

`backend/tests/test_rls_role_split.py`（11 例）：

- `test_role_is_not_superuser_and_cannot_bypass_rls` — 权限位硬断言
- `test_tenant_isolation_between_two_tenants` — 用与业务表完全一致的策略建探针表，
  以 `ekb_app` 连接、设置 GUC=租户 A，断言**只能看到 A 的行**
- `test_every_tenant_table_has_rls_migration` — 静态覆盖门禁：模型里任何带
  `tenant_id` 的表若没有对应 RLS 迁移，测试直接红（这就是本次能揪出 12 张缺口的原因）
- `test_unset_guc_is_fail_open_today` — 把「GUC 未设置时全表可见」写成
  `xfail(strict=True)` 的可执行记录。**关掉 fail-open 后它会变成 XPASS 并失败**，
  强制改的人同步更新本文档，而不是默默改语义

### 9.4 仍未做的（第 2 步，需决策）

fail-open 分支还在：`current_setting('app.tenant_id', true) IS NULL` 时全表可见。
关掉它意味着下面这些「没有请求上下文」的路径会立刻失效，需要先给白名单通道：

| 路径 | 现状 |
|------|------|
| Celery worker（解析/索引/巡检） | 多数任务不设 GUC，依赖 fail-open 读写全租户数据 |
| `scripts/*`（迁移、评测、回填） | 无请求上下文 |
| 单租户兜底模式（未启用多租户的部署） | `request.state.tenant_id` 为空 |
| 系统级后台任务（补偿扫描、配额统计） | 显式跨租户查询 |

建议路径：先给 Celery `worker_process_init` 和脚本入口统一加「显式跨租户」开关
（`SET LOCAL app.tenant_id = ''` 或改用带 GUC 的系统角色），把 fail-open 分支
改成 fail-closed，再逐步收紧。**在完成这一步前，RLS 只能算「有 GUC 的请求路径
受保护」，不是全链路保护。**

---

## 十、第 2 步已落地：fail-closed 与显式跨租户通道（2026-11-10）

第九章做的第 1 步（拆非 superuser 运行角色）只让 RLS **能够**生效。
真正把它变成防线的是这一步：**GUC 缺失时不再放行**。

### 10.1 策略从 fail-open 改成 fail-closed

```sql
-- 之前：GUC 没设置 → 第一个分支恒真 → 全表可见
USING (current_setting('app.tenant_id', true) IS NULL
       OR tenant_id::text = current_setting('app.tenant_id', true))

-- 现在：GUC 没设置 → 两边都是 NULL → 一行都看不见
USING (current_setting('app.tenant_id', true) = '*'
       OR tenant_id::text = current_setting('app.tenant_id', true))
WITH CHECK (同上)
```

迁移 `e8f9a0b1c2d3`，按 ``information_schema`` 动态遍历所有含 ``tenant_id``
的表，避免"写死 48 张表，以后新表又零防护"。

比较用 ``tenant_id::text`` 而不是 ``::uuid``：GUC 为 ``'*'`` 时
``'*'::uuid`` 会直接抛 ``invalid input syntax for type uuid``，策略从
"报错"变成"放行"的反面 —— 跨租户维护脚本会全线失败。

### 10.2 三种身份，没有第四种

| 身份 | 入口 | GUC |
|------|------|-----|
| 某个租户 | `tenant_scope()` / `get_db_session` | `<uuid>` |
| 跨租户（系统路径） | `system_scope(reason=...)` / `get_system_db_session` | `*` |
| 无身份 | 什么都不设 | 空 → **拒绝所有行** |

所有 GUC 写入收敛到 `app/core/tenant_context.py` 一个模块。
第 1 步之前这些 SQL 散在 `database.py`、`chat_service`、`memory_manager` 三处。

`system_scope` 强制要 `reason`，并写 `tenant.cross_tenant_scope` 审计日志。
跨租户本身不是错误，但必须是**可数的、有理由的** —— 否则就是把 fail-open
换个文件名放回来。

### 10.3 Celery：53 个会话调用点，0 个"无身份"

改之前有 52 处 `task_db_session()` 都在裸跑（依赖 fail-open 才能读到数据）。
现在：

```
显式声明（tenant_id= / cross_tenant_reason= / resolve）  39
靠 ContextVar 从任务参数传递                              14
无身份来源                                                 0
```

- **ContextVar 传递**：`celery_app.py` 的 `task_prerun` 信号按函数签名从任务
  参数里取出 `tenant_id` 存进上下文，`task_db_session()` 无参时自动读取。
  这样不用改几十个函数签名和它们的调用方。
- **按主键反查**：拿不到 `tenant_id` 的任务（历史队列消息）用
  `resolve_task_tenant()`——在 `system_scope` 内只 `SELECT tenant_id` 一列，
  查完立刻退出跨租户作用域。暴露面被压到"按主键查归属"这一条查询上，
  而不是让整个任务跑在跨租户下。
- **系统任务显式跨租户**：定时巡检、outbox flush、日摘要这些工作对象本来就是
  全租户数据，给它们 `cross_tenant_reason="<module>.<function>"`。

### 10.4 三个实现坑（都是改 fail-closed 时才暴露的）

**① `SET LOCAL` 会被 commit 清掉。** 原实现用 `SET LOCAL app.tenant_id = ...`，
作用域是当前事务。业务代码只要 `commit()` 过一次，GUC 就失效 —— 之后那次查询在
fail-open 下看到全部租户（旧行为），在 fail-closed 下返回空集（表现为"偶尔查不到
数据"，极难定位）。改用 `set_config(name, value, false)`（session 级）。

**② `RESET` 对自定义 GUC 是"设为空串"，不是"恢复未定义"。** 验证工具第一版用
`RESET app.tenant_id` 来模拟"没有租户"，结果 `current_setting` 返回 `''` 而非
NULL —— 而 fail-open 策略下 `''` 不匹配任何行。于是"无 GUC 是否越权可见"这条
最关键的检查，在**策略最松的库上假绿**。现在用**全新连接**做这项探测，只有
从未设置过的连接 `current_setting` 才是 NULL。

**③ 连接池会把 GUC 带到下一个请求。** `app.tenant_id` 是连接级状态，主引擎是
QueuePool。归还时若 GUC 还留着上一个请求的租户（最糟是 `'*'`），下一个请求即使
一个租户都没设也会带着它。现在两道收尾：`get_db_session` 的 finally 先
`clear_tenant` **再 commit**（直接 close 等价 rollback，RESET 会被一并撤销），
加上 engine 的 `checkin` 事件在 DBAPI 层再 `RESET` 一次，失败则**丢弃连接**
而不是放回池。

### 10.5 验证工具（生产可直接跑，只读）

```bash
# 必须用应用运行角色连接，超级用户会绕过 RLS，测不出任何问题
backend/.venv/bin/python backend/scripts/verify_rls_isolation.py \
    --url "postgresql://ekb_app:***@<host>:5432/ekb" --only-critical
```

对每张表输出「无 GUC / 租户 A / 异租户行数 / 跨租户」四列并判定。
本地实测（双租户、3 文档、4 切片）：

```
表名                    无GUC   租户A   异租户   跨租户  结论
documents                   0       2       0       3  OK
document_chunks             0       2       0       4  OK
knowledge_bases             0       1       0       2  OK
users                       0       1       0       2  OK
✓ 7 张表全部通过：无 GUC 不可见、租户间零泄漏、跨租户通道可用
```

工具做了反向验证：把策略 `downgrade` 回 fail-open，脚本立刻报
`无 GUC 却可见 3 行（策略仍 fail-open）`。

### 10.6 上线前必做的两件事

**① NULL tenant_id 必须回填。** 48 张表里 **47 张的 `tenant_id` 允许为 NULL**。
fail-closed 下 `tenant_id::text = guc` 对 NULL 恒为 NULL，这批行对**任何身份**
都不可见（包括跨租户模式也只看得到非 NULL 行）。不报错、不告警，数据凭空消失。

```bash
backend/.venv/bin/python backend/scripts/check_null_tenant_rows.py \
    --url "postgresql+asyncpg://<owner>:***@<host>:5432/ekb" --emit-sql
```

退出码 1 = 有 NULL 行，阻塞上线。脚本会列出每张表的行数并给回填模板。

**② `.env` / compose 的 `DATABASE_URL` 换成 `ekb_app`。** 见 9.2 三行部署步骤。

### 10.7 已知边界（这一版没解决的）

| 边界 | 影响 | 状态 |
|------|------|------|
| 测试库跑在 superuser 上 | CI 里 RLS **永远不生效**，5282 个测试测不到 RLS 回归 | 靠 `test_rls_fail_closed.py` 的静态门禁 + `verify_rls_isolation.py` 的生产验证补 |
| 87 个端点没有认证依赖 | fail-closed 下它们查不到数据（比泄漏好），但本该补认证 | 独立安全问题，不属本轮 |
| `tenant_id` 可空 | 见 10.6 ① | 靠上线前检查脚本卡住 |
| 单租户部署 | 无 JWT 的请求没有租户，全部查不到 | 需要显式配置单租户 ID，**没有隐式兜底**了 |

### 10.8 守住它的测试

`backend/tests/test_rls_fail_closed.py`（15 例）：

- `test_strict_predicate_rejects_unset_guc` — 迁移谓词里出现 `IS NULL` 就红。
  已做反向验证：注入 fail-open 分支 → 精确变红。
- `test_comparison_uses_text_cast` — 防止有人把 `::text` 改回 `::uuid`
- `test_every_task_session_declares_identity` — AST 扫描 `tasks/`，
  任何"无身份还查库"的 `task_db_session()` 直接红
- `test_cross_tenant_tasks_carry_reason` — 跨租户理由必须是非空字面量
- `test_no_tenant_sets_no_guc` — 无租户时不许替调用方兜底
- `test_invalid_tenant_raises` — 非法 tenant_id 必须炸，不能被当成"没有租户"放行

### 10.9 第 3 步：连接池 GUC 泄漏防护

已在**单连接复用**场景实测通过（`pool_size=1` 强制复用）：

```
session1 (GUC='*')   documents = 3
session2 (未设 GUC)  documents = 0     backend_pid=4340
session3                              backend_pid=4340  ← 连接确实被复用
checkin RESET 失败次数 = 0
```

第二个会话没有设置任何 GUC，看到的仍是 0 行 —— 说明 `checkin` 里的
DBAPI 层 `RESET` 在 asyncpg 适配层工作正常，GUC 没有串到下一个会话。

**没做的**：高并发压测。上线后建议观察 `db.tenant_guc_reset_failed` 日志，
非零说明某些情况下重置失败（那时连接会被丢弃，功能仍然正确但性能下降），
需要改成 `poolclass=NullPool` 或显式 connection wrapper。

---

## 十一、公开端点收口与性能结论（2026-11-11）

### 11.1 先纠正两个此前的错误结论

| 旧结论 | 实测/复核结果 |
|---|---|
| 「87 个端点无认证」 | **错**。319 个路由函数里只有 10 个无认证依赖，且都该公开 |
| 「9 个公开端点」 | **少算 1 个**。旧 AST 把 `OAuth2PasswordRequestForm` 里的 `oauth2` 当认证依赖，漏了 `auth.login`。精确清单是 10 个，见 `tests/test_public_endpoint_contract.py` |
| 「`queue_monitor_service` 用独立 `session_factory()`，统计恒 0」 | **错**。它是构造注入 `AsyncSession`，来自 `get_db_session`（有 JWT → 有 GUC），不需要改。已加静态门禁锁住这一点 |

### 11.2 性能分支的收尾结论：走 B，策略一字不动

100 万行双租户压测里 103ms 的全表扫描只在 `SELECT count(*)` 这类
**无选择性条件的租户级聚合**上出现。静态清点（`307` 个含 select/聚合的函数）
发现 `document_chunks` 全仓只有 `ChunkService` 查询，且每条都带 `doc_id`
或主键 + 显式租户过滤，**零租户级扫描**；141 个租户级扫描全部落在
`documents` / `search_logs` / `usage_records` / `qa_answers` 等比 chunks
小几个数量级的表上。按实测线性缩放，万行级 <1ms。

因此 **A′（收敛反查 + 换角色 + 改谓词 + 补索引）不执行**，代价（独立连接池、
事务边界语义变更、约 20 处反查回归、48 张表策略迁移）换不到实际收益；
**B 也不必做缓存/物化**，因为目标查询在生产代码路径上不存在。
`e8f9a0b1c2d3` 的谓词与 `guc='*'` 跨租户通道保持原样。

**不确定性**：没有生产行数，`documents` 若达几十万行需重评；静态扫描有误报。
上线前用 `pg_stat_statements` 或慢查询日志实测一次，不要靠静态推断定架构。

### 11.3 本轮修复的四类遗留风险

1. **三个公开端点的租户上下文**
   - `widget_ask`：`WidgetTokenService.create_token` 现在**强制**要求
     `tenant_id`（keyword-only），签进 HMAC 载荷；签发时取自已认证请求的
     `request.state.tenant_id`。`widget_ask` 校验后 `apply_tenant(db, claims.tenant_id)`，
     旧令牌（无该字段）直接 401。**身份来自服务端签名令牌，不是请求头**。
   - `oidc_login`：只拼授权 URL、不查库，直接删掉 `get_db_session` 依赖，
     `OIDCService()` 允许 `db=None`。
   - `oidc_callback`：改挂 `get_system_db_session`（登录前按邮箱/sub 找用户
     与 `auth.login/register` 同构）。同时补上 `OIDCService.login_or_register`
     签发 JWT 时**漏掉的 `tenant_id`** —— 否则用户能登录但后续请求全程
     fail-closed；顺带修掉并发注册分支里未定义的 `user_repo`（会 `NameError`）。

2. **`external_webhooks` 的客户端可控租户**
   - `?tenant_id` 降级为**可选的候选筛选提示**，绝不再作为派发身份；
   - 凭证查询放进显式 `system_scope(reason="webhook.resolve_credentials")`
     （旧版用裸 `async_session_factory`，生产 RLS 下恒查 0 行 → 端点恒 401）；
   - 逐个候选验签，**验签命中的那条凭证的 `tenant_id` 才是身份**；
   - 无签名密钥的凭证默认拒绝（`WEBHOOK_ALLOW_UNSIGNED=false`），
     否则任何匿名 POST 都能触发同步；
   - 私有部署凭证 `tenant_id` 为 NULL 时需显式配置
     `WEBHOOK_DEFAULT_TENANT_ID`，否则拒绝派发，不静默丢进无 GUC 的会话；
   - 非法 query UUID 只忽略，不再抛 500。

3. **`high_risk_audit_service` 的自建会话**
   4 个方法全部新增 `tenant_id` 参数并 `apply_tenant`；`observability.py`
   三个管理员端点传 `user.tenant_id`。不传时**保持 fail-closed 并打 warning**，
   绝不兜底到跨租户 —— 审计服务不能成为绕过隔离的后门。

4. **APISIX 的 `os.getenv` 降级路径**
   外部资料确认：nginx 默认清除 worker 继承的全部环境变量（默认只保留 `TZ`），
   `os.getenv("TENANT_ROOT_DOMAIN")` 必须配合 `config.yaml` 的
   `nginx_config.envs` 声明才有效，否则恒为 nil —— 于是 `X-Tenant-Domain`
   永不注入，`limit-req` 的 key 退化成空串，**全站共用一个限流桶**。
   现改为网关清洗三个租户头后直接注入 `ngx.var.host`（完整 Host）：
   不依赖任何环境变量、不需要 `env` 声明、key 恒有值。后端
   `extract_domain_from_request` 在没有该头时本来就返回完整 hostname，
   语义无倒退。`docker-compose.apisix.yml` 里无效的 `TENANT_ROOT_DOMAIN`
   透传已删除。

### 11.4 第 3 步：10 个公开端点逐项确认

| 端点 | 租户通道 | 凭据/签名 | 限流 |
|---|---|---|---|
| `auth.register` | `get_system_db_session` | `REGISTRATION_ENABLED` 开关 | 网关 + 全局中间件 |
| `auth.login` | `get_system_db_session` | 密码校验 | 网关 + 全局中间件 |
| `oidc.login` | 无 DB | OIDC state / 一次性 code | 网关 + 全局中间件 |
| `oidc.callback` | `get_system_db_session` | 一次性 code | 网关 + 全局中间件 |
| `widget.ask` | HMAC 令牌里的 tenant_id | Widget token HMAC | 网关 + 进程内限流 |
| `webhooks/external` | 验签命中的凭证行 | HMAC（默认强制） | 网关 + 全局中间件 |
| `im.wecom_callback_verify` | 无 DB | 企微 SHA1 | 网关 + 全局中间件 |
| `im.wecom_callback_events` | 无 DB | **本轮补上**企微 SHA1 | 网关 + 全局中间件 |
| `i18n.locales` / `i18n.messages` | 无 DB | 纯静态只读 | 网关 + 全局中间件 |

核对时发现 **POST 企微事件回调此前完全没验签**，任何匿名 POST 都能拿 200；
虽当前不查库不派发，但接上事件总线就是无门槛写入口，已补齐。同时给
GET/POST 加了 `WECOM_BOT_TOKEN` 配置缺失守卫 —— 空 token 的 sha1 仍可算，
不能当成「开发环境放行」。

清单固化为 `tests/test_public_endpoint_contract.py`：AST 扫描出的公开端点
集合必须**精确等于**清单，新增/删除一律变红，强制重新回答三个问题。

### 11.5 验收与仍未解决的上线边界

本地回归：全量 pytest、`docker compose config`、迁移单头与漂移门禁。
网关 Lua 片段只做了 YAML/静态门禁校验，**未在真实 APISIX 容器实测**
（本机无镜像）；`ngx.var.host` 是 nginx 内建变量，不依赖环境变量这一层
不确定性已消除。

仍未解决（沿用第十章）：
- `check_null_tenant_rows.py` 是上线阻塞项；47/48 表 `tenant_id` 可空，
  NULL 行在 fail-closed 下对所有身份不可见。
- CI 测试库用 superuser，RLS 不生效，双角色隔离只能在本地/预发实测。
- 单租户部署没有隐式兜底，必须显式配置租户 ID（本轮新增
  `WEBHOOK_DEFAULT_TENANT_ID` 只覆盖 webhook 一条链路）。
- `tenant_domain_to_id` 仍没有调用方传入，`tenants` 表的子域映射未启用；
  网关注入的 `X-Tenant-Domain` 目前只作语义标识与限流维度。
- `db.tenant_guc_reset_failed` 需上线观察。

