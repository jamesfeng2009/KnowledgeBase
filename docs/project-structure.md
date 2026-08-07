# 项目结构

## 项目结构

```
EnterpriseKnowledge/
├── backend/                          # 后端（FastAPI + Celery）
│   ├── app/
│   │   ├── api/v1/                   # 内部 API（32 个路由模块，JWT 认证）
│   │   │   ├── approvals.py          # P1 工具审批 REST（GET pending / POST approve / POST reject）
│   │   │   ├── models.py             # P2 模型选择 REST（GET models / PUT session model）
│   │   │   └── ...                   # chat / knowledge / documents / search 等
│   │   ├── api/openapi/v1/           # 开放接口（6 类能力，API Key 认证）
│   │   ├── agents/                   # 多 Agent 协作（CrewAI）
│   │   │   ├── base.py                  # Agent Loop 基类（think→execute→reflect 循环）
│   │   │   ├── crew.py                  # CrewAI 编排（结构化通信 + 原始需求透传）
│   │   │   ├── reviewer_agent.py        # P2: 高风险操作对抗审查 Agent
│   │   │   └── ...                      # qa / workflow / action agent 等
│   │   ├── connectors/               # 企业连接器（OA/ERP/CRM/Mail）
│   │   ├── context/                  # 对话上下文工程（P3-P4）
│   │   │   ├── focus_tracker.py         # P3-A: 焦点追踪（TopicTracker + ConversationFocus + 焦点历史栈）
│   │   │   ├── coreference_resolver.py  # P3-A: 指代消解（规则 + LLM + 历史注入 + 焦点栈）
│   │   │   ├── context_selector.py      # P3-B: 上下文选择器（语义相似度 + 时间衰减 + 去重）
│   │   │   ├── conversation_summarizer.py # P3-C: 对话摘要（分层压缩 + 关键信息保留）
│   │   │   ├── context_budget.py        # P3-E: 上下文预算管理（Token 上限保护）
│   │   │   ├── drift_detector.py        # P4-A: 漂移检测（三级策略：规则→Embedding→置信度衰减）
│   │   │   ├── contradiction_detector.py # P4-B: 矛盾检测（用户陈述/回答-知识库/文档间三场景 + check_answer_consistency 接线）
│   │   │   ├── high_risk_detector.py   # 高风险信息核验（金额/日期/法律条款检测 + 来源一致性校验）
│   │   │   ├── preference_drift_detector.py # P4-F: 偏好偏移检测（纯规则零 Token）
│   │   │   ├── retrieval_matcher.py     # P4-D: 检索匹配检测（embedding 相似度）
│   │   │   └── repetition_detector.py   # P4-G: 重复提问检测（cosine > 0.85，复用 embedding）
│   │   ├── core/                     # 模块注册表 + 权限
│   │   ├── llm/                      # LLM Provider 抽象层（Anthropic / DashScope / vLLM）
│   │   │   ├── dashscope_provider.py # 通义千问 Provider（saas_dashscope 模式，OpenAI 兼容）
│   │   │   ├── model_config.py       # P2 models.json 配置加载器（lru_cache + deploy_mode 过滤）
│   │   │   └── ...                   # anthropic / vllm / embedder / factory 等
│   │   ├── mcp/                      # MCP 工具协议 + StreamableHTTP 传输层
│   │   │   ├── server.py                # MCP Server（工具注册与分发）
│   │   │   ├── client.py                # MCP Client（JSON-RPC 协议方法）
│   │   │   ├── protocol.py              # JSON-RPC 2.0 协议编解码（对齐 MCP 2026-07-28）
│   │   │   ├── streamable_http.py       # StreamableHTTP 传输层（同步/SSE 流式路由）
│   │   │   └── task_store.py            # 长耗时任务持久化状态管理（Redis/进程内降级）
│   │   ├── memory/                   # 四级记忆引擎
│   │   │   ├── memory_manager.py        # 四级记忆编排器（关键决策持久化 + LLM 事实提取）
│   │   │   ├── mem0_manager.py          # L3 Mem0（cosine 语义检索 + Embedding 双索引 + TTL）
│   │   │   └── ...                      # checkpoint / graphiti_manager 等
│   │   ├── models/                   # SQLAlchemy ORM 模型
│   │   ├── observability/            # LangFuse 追踪 + LLM Judge + request_id 关联
│   │   ├── rag/                      # Agentic RAG 引擎（含 vector_store 适配层 + 跨模态检索）
│   │   │   ├── engine.py             # Agent Loop（含 Find Skills 按需加载）
│   │   │   ├── skill_registry.py     # Skill 注册表（轻量索引 + 按需加载）
│   │   │   ├── skill_finder.py       # Find Skills 匹配引擎（中英文分词 + 多维评分）
│   │   │   ├── tool_guard.py         # MCP 工具调用守卫（deny-by-default + HITL 三态守卫 + P1 会话级控制）
│   │   │   └── ...                   # chunker / retriever / reranker / generator 等
│   │   ├── repositories/             # 数据访问层
│   │   ├── schemas/                  # Pydantic 数据模型
│   │   │   ├── approval.py           # P1 工具审批 Schema（ToolApprovalResponse / ApprovalActionRequest）
│   │   │   └── ...                   # conversation / knowledge / user 等
│   │   ├── services/                 # 业务逻辑层（29 个服务）
│   │   │   ├── approval_service.py   # P1 审批服务（CRUD + 会话级缓存 + 重启恢复）
│   │   │   ├── model_selection_service.py # P2 模型选择服务（两级优先级：session > default）
│   │   │   └── ...                   # permission / search / notification 等
│   │   ├── utils/                    # 工具（crypto/logger/sse/minio_client）
│   │   ├── asr/                      # ASR 语音转写（Whisper/FunASR）
│   │   ├── vlm/                      # 视觉语言模型
│   │   ├── video/                    # 视频处理（ffmpeg 音轨提取 + 关键帧抽取）
│   │   ├── document/                 # 文档解析器
│   │   │   ├── docling_parser.py     # Docling 统一解析（primary，MIT，→ HTML）
│   │   │   ├── pdf_parser.py         # PDF 解析（fallback，pymupdf + 表格 + 图片上传/小图过滤 + VLM + 扫描页 OCR）
│   │   │   ├── docx_parser.py        # DOCX 解析（fallback，python-docx + 标题层级 + 列表结构 + 表格 + 图片上传 + 页眉页脚 + 分页检测）
│   │   │   ├── pptx_parser.py        # PPTX 解析（fallback，python-pptx + GROUP 递归 + 图表数据 + 图片上传/小图过滤 + 备注）
│   │   │   ├── xlsx_parser.py        # XLSX 解析（fallback，openpyxl + pandas 降级 + sheet→HTML + 列宽对齐）
│   │   │   ├── image_storage.py      # 图片对象存储（零依赖尺寸解析 + 小图过滤 + MinIO 上传）
│   │   │   ├── factory.py            # 解析器工厂（Docling 优先 → 原有降级）
│   │   │   └── base.py              # 解析器基类（ParsedSection + sections_to_text + 分页分隔符）
│   │   ├── utils/                    # 工具模块
│   │   │   ├── migration.py          # Alembic 迁移运行器（run_migrations / stamp_head）
│   │   │   ├── minio_client.py       # MinIO 对象存储客户端
│   │   │   └── logger.py             # 结构化日志
│   │   ├── eval/                     # 离线评测系统（数据集+Runner+回归基线+CLI）
│   │   ├── config.py                 # 配置管理（Pydantic V2 + field_validator + model_validator）
│   │   ├── database.py               # 数据库会话
│   │   ├── deps.py                   # 依赖注入
│   │   └── main.py                   # FastAPI 入口（lifespan → alembic upgrade head）
│   ├── alembic/                      # Alembic 迁移脚本
│   │   ├── env.py                    # 异步引擎 + 自动导入模型 + compare_type
│   │   └── versions/                 # 迁移版本（17 个，init 27 张表 + 多租户/审核/测试平台 6 张表 + 知识回流层 3 张表 + AI 评测 + 记忆 + 审计等）
│   ├── config/
│   │   └── models.json               # P2 模型配置文件（7 个模型 × 4 种部署模式，Git 管理）
│   ├── tasks/                        # Celery 异步任务
│   ├── tests/                        # 测试（3061 项）
│   ├── celery_app.py                 # Celery 入口
│   └── requirements.txt
├── collab-service/                   # Yjs 协作服务（Node.js + TypeScript）
│   ├── src/
│   │   ├── index.ts                  # 服务入口
│   │   ├── connection.ts             # WebSocket 连接管理
│   │   ├── persistence.ts            # PostgreSQL 持久化
│   │   ├── awareness.ts              # 协作者状态管理
│   │   └── comments.ts               # 评论通知
│   └── package.json
├── frontend/                         # 前端（Astro 5 + React 19 + TypeScript 5.6）
│   ├── src/
│   │   ├── components/               # 组件库（分层架构，CSS 变量驱动）
│   │   │   ├── common/               # 通用组件（12 个）
│   │   │   │   ├── StatCard.astro    # 统计卡片（标题/数值/图标/趋势）
│   │   │   │   ├── PageHeader.astro  # 页面头部（标题/面包屑/操作区）
│   │   │   │   ├── EmptyState.astro  # 空状态（图标/标题/描述/操作按钮）
│   │   │   │   ├── Avatar.astro      # 头像（首字母/图片/尺寸变体）
│   │   │   │   ├── Badge.astro       # 徽章（颜色变体/圆点指示器）
│   │   │   │   ├── Button.astro      # 按钮（变体/尺寸/加载状态）
│   │   │   │   ├── Modal.astro       # 模态框（遮罩/关闭/尺寸变体）
│   │   │   │   ├── Tabs.astro        # 标签页（ARIA tablist 语义）
│   │   │   │   ├── Tag.astro         # 标签（可关闭/颜色变体）
│   │   │   │   ├── Toast.astro       # 轻提示（类型/自动关闭）
│   │   │   │   ├── NotificationActions.astro  # 通知操作按钮组
│   │   │   │   ├── NotificationTabs.astro     # 通知标签页（全部/未读）
│   │   │   │   └── index.ts          # 统一导出
│   │   │   ├── knowledge/            # 知识库组件（2 个）
│   │   │   │   ├── KbCard.astro      # 知识库卡片（文档数/可见性/操作菜单）
│   │   │   │   ├── DocItem.astro     # 文档列表项（图标/状态/解析进度）
│   │   │   │   └── index.ts
│   │   │   ├── admin/                # 管理后台组件（3 个）
│   │   │   │   ├── HealthRing.astro  # 健康度环形图（SVG 圆弧 + 渐变）
│   │   │   │   ├── AuditStep.astro   # 审核步骤（状态连线/时间轴）
│   │   │   │   ├── UserTableRow.astro # 用户表格行（角色徽章/状态切换）
│   │   │   │   └── index.ts
│   │   │   ├── settings/             # 设置组件（4 个）
│   │   │   │   ├── ApiKeyTable.astro # API 密钥表格（创建/撤销/复制）
│   │   │   │   ├── LlmConfigForm.astro # LLM 配置表单（Provider/模型/参数）
│   │   │   │   ├── SystemForm.astro  # 系统设置表单（站点名/上传限制/功能开关）
│   │   │   │   ├── TenantCard.astro  # 租户卡片（套餐/模块门控/用量）
│   │   │   │   └── index.ts
│   │   │   ├── management/           # React Island 组件（文档智能处理）
│   │   │   │   ├── ScanProcessor.tsx # 扫描件 OCR（上传 PDF → VLM 识别 → 入库）
│   │   │   │   ├── IntelligencePanel.tsx      # 智能处理面板（摘要/标签/分类）
│   │   │   │   ├── CollabEditor.tsx           # Yjs 协同编辑器
│   │   │   │   ├── ActionItemList.tsx         # 行动项列表
│   │   │   │   ├── AutoTagEditor.tsx          # 自动标签编辑器
│   │   │   │   ├── SummaryCard.tsx            # 摘要卡片
│   │   │   │   └── ...                        # ConnectionStatus / EditorToolbar 等
│   │   │   ├── chat/                 # 对话组件（React）
│   │   │   │   ├── ChatMessage.tsx   # 对话消息（用户/助手/引用标注）
│   │   │   │   ├── ChatInput.tsx     # 输入框（多行/快捷指令/附件）
│   │   │   │   └── CitationPanel.tsx # 引用面板（来源文档/高亮片段）
│   │   │   └── index.ts
│   │   ├── pages/                    # 页面（44 个，按域分目录）
│   │   │   ├── admin/                # 管理后台（8 页：仪表盘/审核/反馈/健康/报表/标签/用户/可观测性）
│   │   │   ├── auth/                 # 认证（2 页：登录/注册）
│   │   │   ├── chat/                 # 对话（3 页：对话/历史/Agent）
│   │   │   ├── knowledge/            # 知识库（7 页：列表/详情/搜索/问答/专家/图谱/时间线）
│   │   │   ├── manage/               # 管理（5 页：编辑器/缺口/知识库/会议纪要/上传）
│   │   │   ├── scenes/               # 场景（2 页：IT 工单/新人入职）
│   │   │   ├── settings/             # 设置（6 页：API/LLM/MCP/系统/租户/Webhooks）
│   │   │   ├── testing/              # 智能测试平台（5 页：概览/知识/计划/用例/评审）
│   │   │   │   └── ai-eval/          # AI 评测（4 页：RAG/裁判/注入/文档解析）
│   │   │   ├── index.astro           # 首页
│   │   │   └── notifications.astro   # 通知中心
│   │   ├── lib/
│   │   │   ├── apis/                 # API 封装层（25 个模块，统一错误处理 + Token 注入）
│   │   │   │   ├── knowledge.ts      # 知识库 + 文档 CRUD + 解析进度 + 搜索
│   │   │   │   ├── webhooks.ts       # Webhook 订阅管理 + OpenAPI Key 存储
│   │   │   │   ├── mcp.ts            # MCP 工具调用（复用 webhooks 的 Key 存储）
│   │   │   │   ├── intelligence.ts   # 文档智能处理（摘要/标签/分类）
│   │   │   │   ├── audit.ts          # 审核工作流
│   │   │   │   ├── users.ts          # 用户权限管理
│   │   │   │   ├── feedback.ts       # 反馈管理
│   │   │   │   ├── apikeys.ts        # API 密钥管理
│   │   │   │   ├── settings.ts       # 系统设置
│   │   │   │   ├── qa.ts             # 问答社区
│   │   │   │   ├── tickets.ts        # 工单系统
│   │   │   │   ├── agents.ts         # Agent 管理
│   │   │   │   ├── analytics.ts      # 知识健康度仪表盘
│   │   │   │   ├── comments.ts       # 文档评论
│   │   │   │   ├── connectors.ts     # 跨系统连接器
│   │   │   │   ├── experts.ts        # 专家发现
│   │   │   │   ├── graph.ts          # 知识图谱
│   │   │   │   ├── multimodal.ts     # 多模态处理
│   │   │   │   ├── notifications.ts  # 通知中心
│   │   │   │   └── tenants.ts        # 租户管理
│   │   │   ├── api.ts                # 基础 HTTP 客户端（getData/postData/putData/delData + 拦截器）
│   │   │   └── auth.ts               # 认证工具（Token 存储/刷新/路由守卫）
│   │   ├── layouts/
│   │   │   └── DefaultLayout.astro   # 主布局（侧边导航 + 面包屑 + Webhook/MCP 导航项）
│   │   └── styles/
│   │       └── global.css            # 全局样式（CSS 变量设计系统：颜色/间距/圆角/阴影）
│   └── package.json
├── docker-compose.yml                # 容器编排
└── README.md
```

---