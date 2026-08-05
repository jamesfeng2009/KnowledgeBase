# 企业知识库项目记忆 (Project Memory)

> 本文件是企业知识库项目的全局规则、约束和约定，适用于当前项目的所有开发工作。
> 最后更新：2026-08-05

## 1. 项目概述

### 项目名称
企业知识库（Enterprise Knowledge Brain）— 基于 Agentic RAG 架构的智能知识管理 SaaS 平台

### 核心定位
- **不是** IM 即时通讯产品（群组讨论已删除，交给飞书/企微打通）
- **不是** 3D/CAD/专业格式处理工具
- **是** 通用办公场景的智能知识管理平台，融合 Agentic RAG + 多模态处理 + MCP 工具执行
- 支持三种部署模式：SaaS 多租户、私有部署、开放接口

### 参考文件
- 主架构设计文档：`enterprise-knowledge-brain/enterprise-knowledge-brain.html`
- DOCX 版本：`enterprise-knowledge-brain/enterprise-knowledge-brain.docx`
- 高保真 UI 原型：`ui-prototype/`（30 个页面，本地预览 `http://localhost:8090`）
- 前端开发 SKILL：`.trae/skills/ekb-frontend/SKILL.md`
- 后端开发 SKILL：`.trae/skills/ekb-backend/SKILL.md`

## 2. 13 层架构（L0-L13）

| 层 | 名称 | 核心组件 |
|----|------|----------|
| L0 | 用户接入层 | Web(Astro) + Mobile + OpenAPI |
| L1 | API 网关层 | APISIX（非自建 Go/Rust 网关） |
| L2 | 认证授权层 | JWT + RBAC(功能权限) + ABAC(数据权限) + LDAP(身份同步) + SSO/SAML |
| L3 | 业务逻辑层 | Python FastAPI |
| L4 | RAG 引擎层 | LangGraph Agentic RAG + Agent Loop + MCP 工具协议 |
| L5 | 多模态处理层 | 双模式：SaaS 用 LLM 原生多模态 / 私有部署用 Pixtral 或 Qwen-VL |
| L6 | 记忆层 | Mem0（当前事实）+ Graphiti（时序图谱） |
| L7 | 知识图谱层 | Neo4j + Graphiti |
| L8 | 检索层 | Milvus（向量）+ OpenSearch（全文）|
| L9 | 存储层 | PostgreSQL + MinIO + Redis |
| L10 | 知识运营层 | 质量控制 + 缺口分析 + 过期预警 |
| L11 | 协作层 | Yjs + Node.js |
| L12 | 反馈闭环层 | 用户反馈 → 知识质量 → RAG 优化 |
| L13 | 开放接口层 | OpenAPI + 企业连接器 + LLM API + Webhook + MCP |

## 3. 技术选型决策（含理由）

### 3.1 前端
- **Astro**：SEO 优先，SSR/SSG 混合，内容站点性能优秀
- **不选 Next.js**：Astro 更适合内容密集型知识库场景
- CSS 设计系统主色：`#4B3FE3`（紫色），全局 CSS 变量定义
- 响应式断点：1200px（大屏）、768px（移动端）

### 3.2 网关
- **APISIX**（Apache 2.0）：80+ 插件、原生 SSE/WebSocket、Lua 扩展
- **不选自建 Go/Rust 网关**：重复造轮子，APISIX 已覆盖所有需求
- **不选 Kong**：APISIX 中文社区更好、插件更丰富

### 3.3 核心引擎
- **Python FastAPI**：LangGraph/LlamaIndex/Mem0/Graphiti/CrewAI 全部 Python 生态
- **不选 Go/Rust**：AI 生态覆盖率 Python 100% vs Go ~30% vs Rust ~10%
- **不选 Litestar**：LangGraph 官方文档、LangServe、LangFuse 全部基于 FastAPI

### 3.3.1 AI 框架分工（各司其职，互不替代）
- **LangGraph**：Agent Loop 编排引擎 — 状态图编排、循环控制（think→retrieve→generate→reflect）、Checkpoint 持久化
- **LlamaIndex**：RAG 数据层框架 — 文档解析、语义分块（HierarchicalNodeParser 父子索引）、混合检索（QueryFusionRetriever）
- **CrewAI**：多 Agent 协作框架 — 复杂任务拆分、Agent 角色分配（QA/Workflow/Action）、顺序执行、结果汇总
- 简单任务直接走 LangGraph 单 Agent Loop；复杂任务由 CrewAI 拆分后每个子任务各走一个 LangGraph Agent Loop

### 3.4 通信架构
- **SSE + WebSocket 混合**（非腾讯 IM、非 Supabase Realtime）
  - SSE：AI 流式输出（LangGraph `.astream()`），单向，无需 WebSocket
  - WebSocket：仅协同编辑（Yjs）和文档评论通知，低频场景
- **不选腾讯 IM**：DAU 计费、vendor lock-in
- **不选 Supabase Realtime**：CDC 导向，非 AI 场景设计
- **知识库内置聊天不是 IM**：是 AI 问答系统，SSE 流式 + 数据库存储 + 可选轻量 WS 通知

### 3.5 数据库
- **PostgreSQL**：主数据库，JSONB 覆盖半结构化数据（GIN 索引）
- **不选 MongoDB**：PG JSONB 足够，避免跨库事务问题
- **OpenSearch**（Apache 2.0）：全文检索，替代 ElasticSearch（许可证合规）
- **Milvus**：向量数据库，支持十亿级向量检索
- **Neo4j**：知识图谱存储，实体关系
- **Redis**：缓存 + 会话 + L1 精确缓存

### 3.6 记忆引擎
- **Mem0**：当前事实存储（KV + Embedding），高频缓存，LangGraph 原生集成
- **Graphiti**（Apache 2.0 开源）：时序知识图谱引擎，追踪知识时间线、实体关系演化、知识过期预警
- **不选 Cognee**：过于复杂，当前需求不需要
- **不选 Zep Cloud**：付费托管服务，Graphiti 自托管即可

### 3.7 对象存储
- **MinIO**：SaaS 主集群首选，S3 兼容、稳定、生态成熟
- **RustFS**：备选，高性能私有部署场景

### 3.8 模型策略（双模式 · 三场景可切换）
- **DEPLOY_MODE 环境变量控制**：saas / private_overseas / private_domestic
- **SaaS 模式**：Claude Sonnet 4.6 API + OpenAI Embedding + Cohere Rerank，零 GPU
- **私有部署·海外（外企）**：Llama 3.3 70B + BGE-M3 + Jina Reranker v2 + Pixtral 12B
- **私有部署·国内（国企）**：Qwen 3 72B + BGE-M3 + BGE Reranker v2 + Qwen2.5-VL
- **Provider 抽象层**：统一接口，环境变量切换，业务代码零改动
- **关键决策**：企业私有部署核心诉求是数据不出企业网络，走 API 违背初衷，必须支持自托管

### 3.9 部署策略
- **不能用 Serverless**：三大硬约束
  1. 长连接需求（SSE/WebSocket）
  2. 有状态服务（Milvus/Neo4j/Redis）
  3. GPU 不可弹性
- 三档部署：
  - MVP（单机 Docker Compose，~¥3k-5k/月）
  - SaaS 标准（双机 K8s，~¥8k-15k/月）
  - 企业私有（3-4 机 K8s，~¥15-30k 一次性硬件）
- **私有部署分层策略**：
  - 前端 (Astro) 永远不走 K8s：编译为静态文件，Nginx 直接托管
  - GPU 模型服务建议不走 K8s：裸机 Docker + GPU 直通更简单
  - 后端微服务走 K8s（中大型）或 Docker Compose（小型）
  - DEPLOY_MODE 环境变量控制：saas / private_overseas / private_domestic

## 4. 微服务拆分（5 个服务）

| 服务 | 语言 | 框架 | 职责 |
|------|------|------|------|
| API 网关 | Lua | APISIX | 路由、限流、认证、SSE/WS 透传 |
| 核心引擎 | Python | FastAPI | RAG 问答、知识管理、权限控制 |
| 异步任务 | Python | Celery | 文档解析、向量化、索引构建 |
| 协作服务 | Node.js | Yjs + ws | 实时协同编辑、文档评论通知 |
| 计费服务 | Python | FastAPI | SaaS 多租户计费、用量统计 |

## 5. UI 原型结构（30 页面，7 模块）

### 页面清单
| 模块 | 页面 | 路由 |
|------|------|------|
| 工作台 | 首页 | `home` |
| 认证 | 登录/SSO/引导 | `login` / `sso-callback` / `onboarding` |
| AI 对话 | 主界面/历史/Agent | `chat` / `chat/history` / `chat/agent` |
| 知识 | 首页/搜索/问答社区/图谱/时间线/文档详情 | `knowledge` / `knowledge/search` / `knowledge/qa` / `knowledge/graph` / `knowledge/timeline` / `knowledge/doc` |
| 管理 | 知识库/上传/协同编辑/会议纪要/知识缺口 | `manage/kb` / `manage/upload` / `manage/editor` / `manage/minutes` / `manage/gaps` |
| 治理 | 看板/健康度/审核/用户/标签/报表/反馈 | `admin` / `admin/health` / `admin/audit` / `admin/users` / `admin/tags` / `admin/reports` / `admin/feedback` |
| 设置 | 租户/API/LLM/系统 | `settings/tenant` / `settings/api` / `settings/llm` / `settings/system` |
| 场景 | 入职/IT 工单 | `scenes/onboarding` / `scenes/it-helpdesk` |

### 设计系统
- 主色 `#4B3FE3`，品牌色系从浅到深：`#EEEBFE` → `#6B5FF7` → `#4B3FE3` → `#3B2FC9`
- 侧边栏深色 `#1A1B2E`，文本 `#8B8D9F`，激活 `#FFFFFF`
- CSS 变量系统：`--primary` / `--success` / `--warning` / `--danger` / `--info`
- 组件类：`btn` / `card` / `badge` / `table` / `tag` / `modal` / `tabs` / `chat-*` / `timeline`
- 原型使用 hash 路由，生产环境需替换为 Astro 路由

### 已删除的设计
- ❌ 群组讨论（IM 功能，交给飞书/企微打通）
- ✅ 替换为：文档评论（异步讨论）+ 问答社区（沉淀型知识问答）

## 6. RAG 工程设计

### 6.1 语义分块（四级优先级）
1. 结构化分块：Markdown/HTML 标签分割
2. 语义分块：相似度 TextTiling 算法
3. 父子索引：小块检索、大块上下文
4. 固定长度兜底：512 tokens

### 6.2 Token 缓存（三级）
- L1 精确缓存：Redis，TTL 1h，key = query hash
- L2 语义缓存：GPTCache，相似度阈值 0.95，TTL 24h
- L3 模型原生缓存：Prompt Caching，session 级

### 6.3 多轮对话
- LangGraph Checkpoint 持久化会话状态
- 支持共指消解、查询重写、增量检索
- 四级记忆管理：短期窗口 + Checkpoint 快照 + Mem0 长期偏好 + 工作记忆上下文

### 6.4 权限过滤顺序（关键！）
**正确顺序**：检索 → 权限过滤 → 重排 → 生成
**错误顺序**：权限过滤 → 检索 → 重排 → 生成（会导致召回率下降）

### 6.5 知识质量控制
- 知识缺口热力图：追踪高频无结果查询
- 过期预警：Graphiti 时序追踪，知识有效期
- 质量评分：文档完整度、引用准确率、用户反馈

## 7. 开发约定

### 7.1 代码规范
- 前端 Astro 组件使用 `.astro` 文件，TypeScript
- 后端 Python 使用 `mypy` 类型检查，`black` 格式化
- API 接口遵循 RESTful，版本前缀 `/api/v1/`
- 所有 API 返回统一格式：`{ code: 0, data: {}, message: "" }`

### 7.2 提交规范
- 前端代码提交到 Astro 仓库
- 后端代码提交到 Go/Python 仓库
- 使用 conventional commits：`feat:` / `fix:` / `docs:` / `refactor:`
- 不允许在同一个 commit 中混合前端和后端改动

### 7.3 调试约定
- 前端调试使用 `alert()`，不使用 `console.log`
- 后端调试使用 `logging`，不使用 `print`
- 必须提供根因分析和分步修复

### 7.4 内容规范
- 移除营销术语（如"免费"、"1080P"）提升可信度
- 漫画/小说内容使用"阅读"而非"播放"
- 标题和元数据保持简洁专业

## 8. 成本控制规则

- Cloudflare 套餐升级推迟到日活用户达到 20,000 时，确保 ROI 为正
- LLM 性格画像刷新限制：免费用户 3 次/总计，Pro 用户 5 次/月
- 弱化 LLM 配额存在感，避免用户感知
- VLM 双模式：SaaS 复用 LLM 原生多模态（零额外成本）/ 私有部署用 Pixtral 或 Qwen-VL 独立服务

## 9. 数据安全规则

- 必须有数据库备份机制
- 不允许任何删除数据库数据的逻辑（使用软删除 `deleted_at` 字段）
- 图片验证：章节图片必须 size > 0 确认采集成功
- 存储规则：下载的图片必须上传到 R2/MinIO 存储

## 10. 实施路线图（P0-P4）

| 阶段 | 时间 | 内容 |
|------|------|------|
| P0 | 1-2 周 | 权限过滤顺序修正 + 热点缓存 + 基础工作台 |
| P1 | 2-4 周 | 知识质量门控 + 反馈闭环 + 文档评论 |
| P2 | 1-2 月 | Agentic RAG（多类型 Agent + MCP 工具）+ 多模态试点 + OpenAPI |
| P3 | 2-3 月 | 知识运营层 + 协作层 + 企业系统连接器 + SaaS 多租户 |
| P4 | 3-6 月 | 商业化（SaaS 上线 + 私有部署版本）|

## 11. IM 打通策略（非自建 IM）

- 飞书打通：注册飞书应用 → 事件订阅回调 → APISIX 网关 → FastAPI 引擎 → 飞书消息 API 回复
- 企业微信打通：配置「应用」而非群机器人（群机器人只能单向 Webhook）
- IM 消息与产品内对话统一会话管理：同一 session_id 贯穿两端
- IM 回复使用交互卡片，"查看详情"按钮跳转回产品原生界面
- IM 内无法实现流式输出，用"思考中..."消息占位

## 12. 关键教训

- 知识库内置聊天不是 IM，是 AI 问答系统，SSE 足够
- 群组讨论是 IM 功能，知识库产品不应自建，交给飞书/企微
- 权限过滤顺序必须正确（检索→过滤→重排→生成）
- Serverless 不适用于此项目（长连接、有状态、GPU）
- Python 是 AI 生态唯一可行语言（LangGraph/LlamaIndex/Mem0/Graphiti 全部 Python-only）
- APISIX 优于自建网关（80+ 插件、Apache 2.0、SSE/WS 原生支持）

## 13. MCP 协议实现（2026-08-01）

### 13.1 架构决策

- **不对齐官方 MCP SDK**：官方 SDK 仍为有状态设计（session/handshake），本项目采用无状态 JSON-RPC 2.0 协议，直接对接 MCP 2026-07-28 规范中的 StreamableHTTP 传输层
- **无状态设计**：每个请求自包含协议版本、身份信息、支持能力，不依赖服务端会话状态
- **JSON-RPC 2.0**：统一请求/响应格式，支持通知（Notification，无响应）、同步响应、SSE 流式响应三种模式
- **长耗时任务**：通过 `tasks/create` / `tasks/get` / `tasks/cancel` 标准方法管理，任务句柄持久化到 Redis

### 13.2 模块职责

| 模块 | 文件 | 职责 |
|------|------|------|
| 协议层 | `protocol.py` | JSON-RPC 2.0 请求/响应模型、错误码、序列化、校验 |
| 传输层 | `streamable_http.py` | 请求路由、同步/异步/SSE 三种响应模式、方法分发 |
| 任务存储 | `task_store.py` | 长耗时任务持久化（Redis Hash 优先，进程内 dict 降级）、TTL 管理 |
| 开放 API | `api/openapi/v1/mcp.py` | `POST /mcp` JSON-RPC 入口 |
| 客户端 | `client.py` | JSON-RPC 协议方法（`jsonrpc_call` / `jsonrpc_tools_list` / `jsonrpc_tools_call`） |

### 13.3 支持的 MCP 方法

| JSON-RPC Method | 说明 | 响应模式 |
|----------------|------|----------|
| `server/discover` | 发现服务器能力 | 同步 JSON |
| `tools/list` | 列出所有可用工具 | 同步 JSON |
| `tools/call` | 调用工具（短耗时直接返回，长耗时 SSE 流式） | 同步 JSON 或 SSE |
| `tasks/create` | 创建长耗时任务 | 同步 JSON |
| `tasks/get` | 查询任务状态 | 同步 JSON |
| `tasks/cancel` | 取消运行中的任务 | 同步 JSON |
| `notifications/initialized` | 客户端初始化通知（无需响应） | 通知 |

### 13.4 关键约束

- 工具调用守卫（`tool_guard.py`）与 MCP 协议层解耦，守卫在 Agent Loop 层执行，协议层不感知守卫逻辑
- 所有 MCP 端点在网关层通过 `X-API-Key` + `mcp:use` scope 认证
- 长耗时任务轮询间隔默认 2000ms，由服务器在 `working` 响应中通过 `poll_interval_ms` 字段告知客户端

## 14. Agent Loop think/Planner 双 prompt 架构与测试隔离（2026-08-05）

### 14.1 两个 prompt 各司其职，不可混用

| 组件 | Prompt 常量 | 文件 | 范式 | 职责 |
|------|------------|------|------|------|
| think 节点 | `_THINK_SYSTEM_STABLE`「决策大脑」 | `app/rag/engine.py:108` | ReAct 单步路由 | 每轮返回 retrieve/tool_call/generate 单关键词，由 `_parse_decision` 解析 |
| PlanManager | `_PLAN_PROMPT`「任务规划专家」 | `app/agents/planner.py:50` | Plan-and-Execute 一次性规划 | 输出 2-4 步 JSON 计划数组，存入 `state["plan_steps"]` |

### 14.2 think 必须用「决策大脑」的硬依据

- **循环结构**：`engine.py:19` 注释「think → [retrieve|tool_call] → think → ... → generate」，think 被多次调用
- **`_parse_decision`**：`engine.py:1822` 解析单个关键词，不是 JSON 数组
- **`max_iterations`**：`engine.py:228` 默认 5 次防无限循环，循环决策才需要迭代上限
- **KV Cache**：`_THINK_SYSTEM_STABLE` 不含动态内容（`engine.py:105-107`），适合 Anthropic Prompt Cache 前缀匹配；`_PLAN_PROMPT` 含 `{query}` 模板变量无法缓存

### 14.3 混合架构执行顺序

`_run_decision_loop`（`engine.py:1281`）执行顺序：
1. 第 1325 行初始化 `state["messages"]`（决策大脑 system + user query）
2. 第 1333 行 `build_initial_plan()` → LLM 调用 1（任务规划专家）生成 plan_steps
3. 第 1345 行 while 循环 → `_think()` → LLM 调用 2+（决策大脑）每轮路由
4. think 第 1761-1767 行将 plan_steps 的 brief 注入 dynamic zone 作为决策参考

### 14.4 测试隔离约定（关键）

**问题**：engine 构造时 `planner=None` 会自动创建 `PlanManager(llm)`（`engine.py:304-308`），复用同一个 `self.llm`。测试 mock `llm.chat` 记录所有调用到 `call_history`，Planner 的 LLM 调用与 think 的混在一起，导致：
- 遍历 `call_history` 断言「所有 system role 调用都是决策大脑」时被 Planner 的「任务规划专家」污染
- Planner 调用消耗 `mock_chat` 的 `responses[0]`，think 拿到错位 response

**约定**：测试 think 的 system prompt 稳定性 / messages 增量 / budget 压缩 / 工具结果去重等**不涉及 Planner 行为**的场景时，`_make_engine` 构造后必须显式禁用 Planner：

```python
engine = AgenticRAGEngine(llm=llm, ...)
engine._planner = None  # 禁用自动创建的 PlanManager，避免其 LLM 调用污染 call_history
```

**已应用的文件**：
- `tests/test_token_optimization.py:132`
- `tests/test_p1_token_optimization.py:92`
- `tests/test_p2_token_optimization.py:92`

**验证**：94 个测试全部通过。

### 14.5 不是 engine bug，是测试隔离缺失

engine 的 Planner 自动实例化是合理设计（复用 LLM 实例），混合架构（Planner 出全局计划 + think 单步路由）正确。问题在于测试想单独验证 think 时无法隔离 Planner 的 LLM 调用，通过 `engine._planner = None` 显式禁用即可。不应改 engine 的 prompt 或 Planner 集成逻辑。
