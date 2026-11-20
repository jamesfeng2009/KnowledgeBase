# 企业知识库大脑（Enterprise Knowledge Brain）

企业级知识库 SaaS 平台，基于 **Agentic RAG** 架构，面向多租户场景提供多模态文档处理、智能问答、协同编辑与知识图谱分析一体化能力。

> **先读这一句**：多租户隔离有部署前置条件。照默认配置直接 `docker compose up -d`，连接用的是 superuser，**RLS 不生效**。要上真实租户数据，先按 [前置条件](#rls-prereq) 做完角色拆分与租户初始化。

## 核心功能

- **Agentic RAG 引擎**：`think → retrieve → tool_call → generate → reflect` 多轮 Agent Loop，RAG 核心层（分块/检索/重排/生成）自研实现，无外部框架依赖
- **多模态文档处理**：Docling 统一解析 PDF/DOCX/PPTX/XLSX/HTML/图片/音频 → 结构化 HTML，图片 VLM 描述、扫描件 OCR、视频 ASR 转写
- **双路混合检索**：Milvus 向量 + OpenSearch 全文召回 + Cohere Reranker 重排，四级语义分块 + 多级记忆（Short-term/Working/Long-term/Graph）
- **多租户隔离**：RLS 行级隔离（fail-closed 策略）+ 非 superuser 运行角色 + 显式跨租户通道 + 密级访问控制
- **MCP 工具协议**：接入 OA/ERP/CRM 实时数据，DangerousToolGuard 守卫 + 工具审批流
- **实时对话智能**：意图路由、漂移检测、矛盾检测、指代消解、偏好识别等 P4 能力
- **智能测试平台**：测试计划/用例/执行/评审 + 知识回流层（知识复利闭环）
- **IT 工单**：工单模型/API/服务全链路，对接智能测试与知识库问答
- **协同编辑**：Yjs + WebSocket CRDT 实时协同，JWT/Cookie 鉴权（令牌不再走 URL query）
- **跨模态检索**：jina-clip-v2 文本与图片向量独立索引，text-to-image 检索
- **工程门禁**：Alembic 迁移漂移检测、前后端 API 契约校验、公开端点契约清单、RLS 真实隔离验证

## 技术栈

| 层级 | 技术选型 |
|------|----------|
| 后端 | FastAPI + SQLAlchemy(async) + Celery + Redis |
| 前端 | Astro 5 + React 19 + TypeScript（静态生成 + React Island，45 页面） |
| 数据库 | PostgreSQL 16 + pgvector、OpenSearch 2.18、Neo4j 5.26、Milvus 2.4（可选） |
| LLM | Anthropic Claude / DashScope 通义千问 / vLLM（Llama 3.3 / Qwen 3） |
| 文档解析 | Docling + pymupdf + python-pptx + python-docx + openpyxl + pandas |
| 部署 | Docker Compose + Nginx（Astro 静态托管）+ APISIX（可选，多租户子域边缘网关） |

APISIX 在 `infra/apisix/`，使用独立 compose 文件启动，不在主 `docker-compose.yml` 中；前端容器用 `nginx:alpine` 托管编译产物。支持 **SaaS（Claude）/ SaaS·国内（通义千问）/ 私有部署（vLLM + TEI）** 三种部署模式。

## 快速开始

### 1. 本地开发 / 单机试用

```bash
git clone https://github.com/jamesfeng2009/KnowledgeBase.git
cd KnowledgeBase                         # 本地目录若叫 EnterpriseKnowledge，cd 成实际名字即可
cp backend/.env.example backend/.env

cd backend && .venv/bin/python scripts/dev_env_check.py   # 自检：连的是哪个库、什么角色
cd ..
docker compose up -d
```

**先看清 PostgreSQL 端口**——仓库里有三套宿主端口，`.env` 只能写一个：

| 启动方式 | 宿主端口 | 说明 |
|---|---|---|
| `docker compose up -d postgres`（自动加载 `docker-compose.override.yml`） | **15432** | 本机开发默认；override 用 `!override` 重映射 |
| `docker compose -f docker-compose.yml up -d postgres` | **5432** | 不加载 override |
| 手工容器 `ekb-pg -p 55432:5432` | **55432** | `.env.example` 当前默认值 |

`core-engine` 容器内由 compose 注入 `@postgres:5432`，不受宿主端口影响；但**宿主机直接跑 uvicorn / pytest 读的是 `.env`**，端口必须与实际实例一致。测试侧 `tests/conftest.py` 会在导入期自动探测 55432/15432，uvicorn 不会。`dev_env_check.py` 退出码 2 即端口或角色不对，会打印可直接粘贴的修正命令。细节见 [本地开发环境](docs/dev-environment.md)。

<a id="rls-prereq"></a>

### 2. 多租户 / 生产部署前置条件（必读）

> ⚠️ **默认 `docker compose up -d` 部署，RLS 是失效的。** Docker 官方 PostgreSQL 镜像用 `POSTGRES_USER` 创建的是 **superuser**，而 PostgreSQL 对 superuser 无条件绕过 RLS——`FORCE ROW LEVEL SECURITY` 只约束表 owner，约束不了 superuser。本仓所有带 `tenant_id` 的表都开了 ENABLE + FORCE，用建库账号连接照样能看到全部租户的数据。
>
> 同时策略已是 **fail-closed**：换成普通角色后，会话没有可信 `app.tenant_id` 时所有行不可见。所以「换角色」和「初始化租户」必须一起做，只做一半会让查询恒返回 0 行。

```bash
cd backend

# 1) 用 owner 连接建非 superuser 运行角色（幂等；--check 可只看现状）
.venv/bin/python scripts/provision_app_role.py --password "$(openssl rand -base64 24)"

# 2) 按脚本输出拆分两条连接：
#    DATABASE_URL          → ekb_app（NOSUPERUSER / NOBYPASSRLS，无 DDL 权限）
#    MIGRATION_DATABASE_URL → owner（表 owner；AUTO_MIGRATE 走这条连接）
#    两者权限要求相反，不可混用、不可都填 owner。

# 3) 上线前回填 NULL tenant_id：48 张租户表里 47 张允许 NULL，
#    fail-closed 策略下这些行对任何身份都不可见（含跨租户通道）
.venv/bin/python scripts/check_null_tenant_rows.py \
    --url "postgresql+asyncpg://<owner>:<pwd>@<host>:5432/ekb" --emit-sql

# 4) 迁移漂移门禁（库需已 alembic upgrade head；迁移必须手写，勿用 autogenerate 生成）
.venv/bin/python scripts/check_migration_drift.py

# 5) 启动后用运行角色验证真实隔离（只读脚本，可在生产执行；superuser 连会测不出问题）
.venv/bin/python scripts/verify_rls_isolation.py \
    --url "postgresql://ekb_app:<pwd>@<host>:5432/ekb"
```

其它容易踩的部署项：

- `REGISTRATION_ENABLED=false` 默认关闭开放注册；新环境需先建首个租户/用户，私有部署可开启。
- 租户身份默认**只认 JWT**（`TRUST_TENANT_ID_HEADER=false`）。`X-Tenant-Id` 请求头只有在显式开启且与网关共享 `TENANT_GATEWAY_TOKEN` 时才被采信；APISIX 会统一剥离客户端自带的租户头。
- 单租户部署也必须显式配置租户身份；`WEBHOOK_DEFAULT_TENANT_ID` 只是外部 webhook 的私有部署兜底，不能代替租户初始化。
- 完整设计与实测结论见 [P0 多租户隔离设计](docs/P0-multi-tenant-isolation-design.md) 第八～十一章，[部署指南](docs/deployment.md)、[迁移规约](docs/migrations.md)。

### 3. 测试与门禁

```bash
cd backend
.venv/bin/python -m pytest --tb=short -q          # 全量回归（真实 PostgreSQL）
.venv/bin/python scripts/check_migration_drift.py  # ORM 模型 vs 迁移 schema 漂移
```

前端 `npm run typecheck`（`astro check`）/ `npm run build`，协同服务 `npx tsc --noEmit`。测试清单见 [测试](docs/testing.md)。

## 文档

### 部署与运维（先看）

| 文档 | 内容 |
|------|------|
| [部署指南](docs/deployment.md) | 三种部署模式、环境变量、端口、Alembic 操作 |
| [本地开发环境](docs/dev-environment.md) | PG 端口与 pytest basetemp 两个坑、自检方式 |
| [数据库迁移规约](docs/migrations.md) | 迁移必须手写、`include_object` 过滤器、漂移门禁 |
| [多租户隔离设计](docs/P0-multi-tenant-isolation-design.md) | RLS 真实边界、角色拆分、fail-closed、公开端点收口 |
| [稳定性与安全加固](docs/security.md) | P0-P3 安全与稳定性修复 |
| [测试](docs/testing.md) | 测试运行方式与覆盖 |

### 架构与功能设计

| 文档 | 内容 |
|------|------|
| [系统架构与 Agent Loop](docs/architecture.md) | 架构设计、Agent Loop、MCP 守卫、RAG 质量守卫 |
| [RAG 检索与记忆架构](docs/rag.md) | 四级语义分块、混合检索管线、四级记忆 |
| [Token 优化与上下文压缩](docs/token-optimization.md) | 14 个 Token 浪费点、六级优化方案 |
| [核心功能设计](docs/features.md) | 门控、通知、Yjs 协同、SSE、工具审批、LLM Provider、限流等 |
| [智能测试平台与离线评测](docs/testing-platform.md) | 智能测试平台、知识回流层、离线评测系统 |
| [对话智能与多 Agent 协作](docs/intelligence.md) | 意图路由、实体注册表、上下文工程、P4 对话智能、工具治理 |
| [项目结构](docs/project-structure.md) | 完整目录树 |

### 阶段性设计稿（历史记录，非现行规约）

[P1 可靠性与数据一致性](docs/P1-reliability-data-consistency-design.md) · [P1/P2 设计提案](docs/P1_P2_Design_Proposal.md) · [P3 上下文工程](docs/P3_Context_Engineering_Design.md) · [P4 实时对话智能](docs/P4_Realtime_Conversation_Intelligence_Design.md) · [P4 Web 混合检索](docs/P4_Web_Hybrid_Retrieval_Design.md) · [D10 素材来源契约](docs/d10-asset-sources-contract.md)

现行规约以上面「部署与运维」为准；设计稿与现行实现冲突时，以实现与测试为准。

## License

Private - All Rights Reserved。本仓库源码未授予任何使用、复制、修改或再分发许可；GitHub / Gitee 上的仓库仅作私有版本管理镜像，不代表开源。授权事宜请联系仓库所有者。
