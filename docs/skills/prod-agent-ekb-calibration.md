# prod-agent × EKB 项目级校准

> 本文档是通用 skill [prod-agent](file:///Users/fengyu/.trae-cn/skills/prod-agent/SKILL.md) 在 EnterpriseKnowledge (EKB) 项目的专属校准。
> 通用 skill 给决策原则，本文档给 EKB 的硬约束绑定。两者冲突时，**EKB 硬约束优先**。

---

## EKB 技术栈绑定

通用 skill 框架无关，EKB 有明确技术栈选择：

| 维度 | EKB 选择 | 对应通用 skill 的"可替换"项 |
|------|----------|----------------------------|
| Agent 编排 | **LangGraph StateGraph**（仅 Agent Loop 层） | prodagent 三模式的 EKB 落地用 LangGraph |
| 数据库 | **PostgreSQL-only**（禁 SQLite） | CheckpointStore / 关系型端口 |
| 向量 | **pgvector**（不引入 Qdrant） | VectorStore 端口 |
| 缓存/限流 | **Redis**（Lua token bucket）+ 内存降级 | CacheStore / LockStore / IdempotencyStore |
| 异步任务 | **Celery** | 长任务 / 后台 run |
| 可观测 | **Langfuse** | SpanExporter |
| 流式 | **SSE** | 事件流输出 |
| 工具注册 | **MCP 内部工具注册表**（非标准 MCP 协议服务器） | Tool 系统 |

## EKB 硬约束 → prod-agent checklist 映射

### 1. Agent Loop 层职责边界（架构决策）

> **EKB 硬约束**：Agent Loop 层必须使用 LangGraph StateGraph 编排状态图（think→execute→reflect 循环）；RAG 核心层（chunker/retriever/reranker/generator）必须手写实现，不使用框架。

**对应 prod-agent**：[architecture-decisions.md](file:///Users/fengyu/.trae-cn/skills/prod-agent/checklists/architecture-decisions.md)

**EKB 落地**：
- LangGraph 只用在 Agent Loop（状态机编排），不渗透到 RAG / 文档解析 / 上下文工程
- LangGraph 也用于记忆引擎 Checkpointer
- RAG 核心层手写，禁用 LangChain RAG / LlamaIndex 等框架
- EKB 实现：[backend/app/agents/](file:///Users/fengyu/Downloads/myproject/workspace/EnterpriseKnowledge/backend/app/agents/)、[backend/app/memory/checkpoint.py](file:///Users/fengyu/Downloads/myproject/workspace/EnterpriseKnowledge/backend/app/memory/checkpoint.py)

### 2. PostgreSQL-only（后端端口）

> **EKB 硬约束**：禁止 SQLite，仅用 PostgreSQL。迁移文件不得含 batch_alter_table / _is_postgresql 等 SQLite 兼容代码，直接用 PostgreSQL DDL。测试环境也必须 PostgreSQL（conftest.py DATABASE_URL = postgresql+asyncpg://）。requirements.txt 禁 aiosqlite。config.py validate_database_url 只允许 postgresql+asyncpg://。

**对应 prod-agent**：[architecture-decisions.md 第2节 后端端口](file:///Users/fengyu/.trae-cn/skills/prod-agent/checklists/architecture-decisions.md)

**EKB 落地**：
- CheckpointStore / EventLog / SessionStore 全部落 Postgres
- 迁移用 `op.create_foreign_key` / `op.drop_constraint` / `op.execute`，不写 SQLite 兼容分支
- 测试用真实 Postgres，不用 SQLite 内存库
- 向量用 pgvector 扩展，不引入 Qdrant

### 3. 多租户隔离（安全护栏）

> **EKB 硬约束**：RAG cache key 必须含 tenant_id，防跨租户信息泄漏。文档访问必须含 classification 检查，防未授权访问 secret 级文档。前端必须 DOMPurify 清洗用户内容再 innerHTML，防 XSS。

**对应 prod-agent**：[security-guardrails.md 第4节 分层工具权限](file:///Users/fengyu/.trae-cn/skills/prod-agent/checklists/security-guardrails.md)

**EKB 落地**：
- 所有 cache key 格式：`{tenant_id}:{key}`，缺失 tenant_id 的 cache 写入直接报错
- 文档检索 SQL 必须带 `WHERE tenant_id = :tenant_id AND classification <= :user_clearance`
- 前端 UGC 渲染前过 DOMPurify，禁止直接 innerHTML
- EKB 实现：[backend/app/middleware/](file:///Users/fengyu/Downloads/myproject/workspace/EnterpriseKnowledge/backend/app/middleware/)（权限过滤中间件）

### 4. 限流降级（容错）

> **EKB 硬约束**：API 限流用 Redis-backed token bucket（Lua 脚本原子操作），Redis 不可用时降级到内存限流，不阻断服务。

**对应 prod-agent**：[resilience.md 第5节 降级策略](file:///Users/fengyu/.trae-cn/skills/prod-agent/checklists/resilience.md)

**EKB 落地**：
- 限流逻辑：优先 Redis token bucket（Lua 原子），Redis 连接失败自动切内存 token bucket
- 降级不静默：日志记录 `rate_limiter=fallback_memory`
- 分层配额：匿名 1 次/天、Free 2 次/天、Pro 10 次/天、Pro+ 100 次/天

### 5. SECRET_KEY 安全（安全护栏）

> **EKB 硬约束**：SECRET_KEY 不得有默认值，启动时若未配置则阻断启动，防 JWT 安全风险。

**对应 prod-agent**：[security-guardrails.md](file:///Users/fengyu/.trae-cn/skills/prod-agent/checklists/security-guardrails.md)（鉴权安全）

**EKB 落地**：
- config.py 启动校验：`SECRET_KEY` 缺失或为默认值 → `raise RuntimeError`，不启动
- JWT 签发用 SECRET_KEY，不硬编码

### 6. RAG 质量守卫（可观测与评估）

> **EKB 硬约束**：RAG 质量守卫必须启用，检索分数和生成 faithfulness 阈值可配置。

**对应 prod-agent**：[observability-eval.md 第4节 黄金评测集](file:///Users/fengyu/.trae-cn/skills/prod-agent/checklists/observability-eval.md)

**EKB 落地**：
- 检索结果按相似度分数过滤（阈值可配置）
- 生成结果过 faithfulness 校验（防幻觉）
- 守卫失败有降级（拒答 / 转人工 / 模板兜底）
- EKB 实现：[backend/app/rag/quality_guard.py](file:///Users/fengyu/Downloads/myproject/workspace/EnterpriseKnowledge/backend/app/rag/quality_guard.py)、[backend/app/rag/engine.py](file:///Users/fengyu/Downloads/myproject/workspace/EnterpriseKnowledge/backend/app/rag/engine.py)

### 7. 长任务与数据保护（预算与恢复）

> **EKB 硬约束**：Long-running tools 返回 task_id + poll_interval_ms，有 /mcp/tasks/{task_id} 状态查询端点。数据库备份机制必须存在，不允许任何删除数据库数据的逻辑。

**对应 prod-agent**：[budget-and-recovery.md 第3节 事件日志](file:///Users/fengyu/.trae-cn/skills/prod-agent/checklists/budget-and-recovery.md)

**EKB 落地**：
- 长任务（爬虫 / 索引重建 / 批量处理）走 Celery，返回 task_id
- 状态查询端点 `/mcp/tasks/{task_id}` 返回状态 + 进度
- 数据库定时备份，代码中不写 `DELETE FROM` 业务数据（只允许软删除 `deleted_at`）
- EKB 实现：[celery_app.py](file:///Users/fengyu/Downloads/myproject/workspace/EnterpriseKnowledge/backend/celery_app.py)、[backend/app/mcp/server.py](file:///Users/fengyu/Downloads/myproject/workspace/EnterpriseKnowledge/backend/app/mcp/server.py)

### 8. MCP 实现现状（工具系统）

> **EKB 硬约束**：当前 MCP 实现是内部工具注册表，非标准 MCP 协议服务器。

**对应 prod-agent**：[security-guardrails.md 第4节 分层工具权限](file:///Users/fengyu/.trae-cn/skills/prod-agent/checklists/security-guardrails.md)

**EKB 落地**：
- 工具注册用内部 registry，不走标准 MCP 协议
- 但副作用分层（LOW/MEDIUM/HIGH）+ HITL 仍需实现
- 未来如迁移到标准 MCP，本约束更新

## EKB 已有的生产基建对照

| prod-agent 基建 | EKB 现状 | 文件 |
|----------------|----------|------|
| 四维硬预算 | ⚠️ 需补全（当前有分层配额，缺 turns/seconds/tokens/cost 四维） | — |
| checkpoint 恢复 | ✅ LangGraph Checkpointer | [checkpoint.py](file:///Users/fengyu/Downloads/myproject/workspace/EnterpriseKnowledge/backend/app/memory/checkpoint.py) |
| 熔断 | ✅ 已实现 | [circuit_breaker.py](file:///Users/fengyu/Downloads/myproject/workspace/EnterpriseKnowledge/backend/app/utils/circuit_breaker.py) |
| 重试 | ✅ 已实现 | [retry.py](file:///Users/fengyu/Downloads/myproject/workspace/EnterpriseKnowledge/backend/app/utils/retry.py) |
| 可观测 | ✅ Langfuse | [langfuse_tracer.py](file:///Users/fengyu/Downloads/myproject/workspace/EnterpriseKnowledge/backend/app/observability/langfuse_tracer.py) |
| RAG 质量守卫 | ✅ 已实现 | [quality_guard.py](file:///Users/fengyu/Downloads/myproject/workspace/EnterpriseKnowledge/backend/app/rag/quality_guard.py) |
| 多租户隔离 | ✅ 已实现 | [middleware/](file:///Users/fengyu/Downloads/myproject/workspace/EnterpriseKnowledge/backend/app/middleware/) |
| SSE 流式 | ✅ 已实现 | [sse.py](file:///Users/fengyu/Downloads/myproject/workspace/EnterpriseKnowledge/backend/app/utils/sse.py) |
| 注入防护 | ⚠️ 需补全（DOMPurify 有，但 LLM 注入防护管道需加固） | — |
| HITL 审批 | ⚠️ 需补全（HIGH 副作用工具审批门禁） | — |
| 黄金评测集 | ⚠️ 需补全 | — |
| 漂移检测 | ⚠️ 需补全 | — |

## EKB 待补齐项（按 prod-agent checklist 优先级）

1. **P0 四维硬预算**：在 Agent Loop 补全 turns/seconds/tokens/cost_usd 四维独立预算 + BudgetLedger
2. **P0 HITL 审批门禁**：HIGH 副作用工具（删除 / 部署 / 数据迁移）挂审批
3. **P1 注入防护管道**：补 L1-L5 五层管道（当前只有 DOMPurify）
4. **P1 黄金评测集**：建立 ≥20 case 的回归集 + CI 集成
5. **P2 漂移检测**：定义轨迹基线 + 定期采样比对
6. **P2 终止校验**：Agent 声称完成时过结构化断言

## 与 EKB 设计文档的关系

| EKB 设计文档 | 对应 prod-agent 基建 |
|--------------|---------------------|
| [docs/architecture.md](file:///Users/fengyu/Downloads/myproject/workspace/EnterpriseKnowledge/docs/architecture.md) | 架构决策 |
| [docs/P0-multi-tenant-isolation-design.md](file:///Users/fengyu/Downloads/myproject/workspace/EnterpriseKnowledge/docs/P0-multi-tenant-isolation-design.md) | 安全护栏 |
| [docs/P1-reliability-data-consistency-design.md](file:///Users/fengyu/Downloads/myproject/workspace/EnterpriseKnowledge/docs/P1-reliability-data-consistency-design.md) | 预算与恢复 / 容错 |
| [docs/P4_Realtime_Conversation_Intelligence_Design.md](file:///Users/fengyu/Downloads/myproject/workspace/EnterpriseKnowledge/docs/P4_Realtime_Conversation_Intelligence_Design.md) | 执行模式 / 上下文工程 |

## 使用方式

开发 EKB 的 Agent 时：

1. 先读通用 [prod-agent SKILL.md](file:///Users/fengyu/.trae-cn/skills/prod-agent/SKILL.md) 走 HARD-GATE
2. 走到每项 checklist 时，回本文档确认 EKB 硬约束是否更严
3. EKB 硬约束更严时，按 EKB 执行
4. 通用 skill 更严时，按通用 skill 执行
5. 两者都满足后，对照"待补齐项"看是否本轮要补
