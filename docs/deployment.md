# 部署指南

## 部署指南

### 环境要求

- Docker + Docker Compose
- Python 3.12+（开发环境）
- Node.js 20+（协作服务开发环境）

### SaaS 模式部署

```bash
# 克隆仓库
git clone https://github.com/jamesfeng2009/KnowledgeBase.git
cd KnowledgeBase

# 配置环境变量
cp backend/.env.example backend/.env
# 编辑 .env 设置 ANTHROPIC_API_KEY、DATABASE_URL 等

# 启动所有服务
docker compose up -d
```

### SaaS·国内模式部署（通义千问，最省钱 demo 方案）

```bash
# 克隆仓库
git clone https://github.com/jamesfeng2009/KnowledgeBase.git
cd KnowledgeBase

# 配置环境变量
cp backend/.env.example backend/.env
# 编辑 .env：
#   DEPLOY_MODE=saas_dashscope
#   DASHSCOPE_API_KEY=sk-xxx  （阿里云百炼平台获取）
#   DASHSCOPE_LLM_MODEL=qwen-turbo  （或 qwen-plus / qwen-max）

# 启动精简服务（demo 只需 7 个容器，无 GPU）
docker compose up -d postgres redis minio opensearch core-engine frontend celery-worker
```

通义千问 Qwen-7B 无限制免费，qwen-turbo/qwen-plus 有新用户免费额度，demo 期间 API 费用接近 0 元。国内直连无需代理。

### 私有部署

```bash
# 设置部署模式为国内私有部署（使用 vLLM + TEI）
export DEPLOY_MODE=private_domestic

# 启动所有服务（含 GPU 模型服务）
docker compose --profile private up -d
```

### 数据库迁移（Alembic）

项目使用 Alembic 管理 PostgreSQL schema 迁移，启动时自动执行 `alembic upgrade head`。

```bash
# 修改 ORM 模型后生成新迁移
cd backend
alembic revision --autogenerate -m "add xxx table"

# 升级到最新版本
alembic upgrade head

# 回滚一步
alembic downgrade -1

# 已有 create_all 建的库切换到 migration — 标记当前为 head，跳过首次迁移
python -c "from app.utils.migration import stamp_head; stamp_head()"
```

**迁移版本历史**：

| 版本 | 日期 | 说明 |
|------|------|------|
| `115a9c06ba4a` | 2026-07-18 | init schema — 27 张基础表（用户/知识库/文档/对话/问答/评论/审核/反馈/租户/记忆/Agent 等） |
| `a1b2c3d4e5f6` | 2026-07-19 | add document parse metadata — Document 表新增 parse_status/parse_warnings/page_count/char_count |
| `b2c3d4e5f6a7` | 2026-07-19 | add tenant_id checkpoint and usage metadata — 多租户隔离字段 + AgentCheckpoint + UsageRecord |
| `c3d4e5f6a7b8` | 2026-07-21 | add tool_approvals table — P1 工具审批持久化 |
| `d4e5f6a7b8c9` | 2026-07-21 | add user_model_preferences table — P2 用户模型选择 |
| `e5f6a7b8c9d0` | 2026-07-21 | add testing platform tables — 智能测试平台 6 张表（test_projects/test_requirements/test_cases/test_reviews/test_plans/test_executions） |
| `f6a7b8c9d0e1` | 2026-07-21 | add knowledge compounding layer — 知识回流层 3 张表（knowledge_assets/compounding_tasks/knowledge_conflicts）+ 测试模型 4 个新增字段 |
| `a7b8c9d0e1f2` | 2026-07-21 | add tenant_id users qa indexes fk — users/qa 表补充 tenant_id 及外键/索引 |
| `b8c9d0e1f2a3` | 2026-07-21 | enable rls tenant isolation — 启用 RLS 行级租户隔离 |
| `c9d0e1f2a3b4` | 2026-07-21 | add tenant_id audit flows and file size — audit_flows 补 tenant_id + 文件大小字段 |
| `d1e2f3a4b5c6` | 2026-07-21 | add content hash to documents — Document 表新增 content_hash |
| `e1f2a3b4c5d6` | 2026-07-24 | add doc parse and judge eval tables — 文档解析评测 + 裁判评测表（ai_eval_doc_parse_* / ai_eval_judge_*） |
| `f2a3b4c5d6e7` | 2026-07-27 | add soft delete to ai eval cases — AI 评测用例软删除 |
| `a3b4c5d6e7f8` | 2026-07-31 | add pgvector to memory facts — memory_facts 增加 pgvector 向量列 |
| `b4c5d6e7f8a9` | 2026-08-04 | add effective window to documents — Document 表新增生效窗口字段 |
| `c5d6e7f8a9b0` | 2026-08-04 | add high risk audit records — 高风险审计记录表（high_risk_audit_records） |
| `d6e7f8a9b0c1` | 2026-08-05 | add tool audit log — 工具调用审计日志表（tool_audit_log） |

**配置项**（`app/config.py`）：

| 配置 | 默认 | 说明 |
|------|------|------|
| `AUTO_MIGRATE` | `True` | 启动时自动 `alembic upgrade head` |
| `AUTO_CREATE_TABLES` | `False` | 兼容旧逻辑，直接 `create_all`（仅 demo） |
| `MAX_UPLOAD_SIZE_MB` | `50` | 单文件上传大小上限（MB），超限返回 413 Payload Too Large |
| `PPTX_IMAGE_UPLOAD_ENABLED` | `False` | PPTX 图片上传 MinIO（关闭时仅 VLM 描述） |
| `PPTX_IMAGE_MIN_SIZE` | `50` | PPTX 图片最小尺寸过滤（剔除图标/装饰小图） |

**Pydantic V2 配置校验**：
- `field_validator`：DATABASE_URL 必须用异步驱动（postgresql+asyncpg）、数值范围、CORS URL 格式
- `model_validator`：部署模式与 API Key 交叉校验、生产环境 SECRET_KEY 安全校验

### 服务端口

| 服务 | 端口 | 说明 |
|------|------|------|
| Frontend | 3000 → 80 | Astro SSR + Nginx |
| Core Engine (FastAPI) | 8000 | 后端 API |
| Yjs Server | 8001 | 协同编辑 WebSocket |
| PostgreSQL | 5432 | 主数据库 |
| Redis | 6379 | 缓存 + Pub/Sub |
| Milvus | 19530 | 向量数据库（可选，VECTOR_STORE=milvus 时启用） |
| OpenSearch | 9200 | 全文检索 |
| Neo4j | 7687 | 知识图谱 |
| MinIO | 9000 | 对象存储 |

### Docker Compose 服务拓扑

| 层级 | 服务 |
|------|------|
| 基础设施 | postgres, redis, minio, opensearch, neo4j（milvus 可选，VECTOR_STORE=milvus 时启用） |
| 应用层 | core-engine, frontend, yjs-server, celery-worker, celery-beat |
| 私有模型 | llm-server (vLLM), embedding-server (TEI), reranker-server (TEI), vlm-server (vLLM) |

---