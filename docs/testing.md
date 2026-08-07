# 测试

## 测试

```bash
cd backend

# 运行全部测试（3061 项）
python -m pytest --tb=short -q

# 运行特定模块测试
python -m pytest tests/test_chunk_optimization.py -v    # RAG 分块优化（含 title_path 前缀 + Overlap）
python -m pytest tests/test_token_optimization.py -v      # P0 Token 优化
python -m pytest tests/test_p1_token_optimization.py -v   # P1 Token 优化
python -m pytest tests/test_p2_token_optimization.py -v   # P2 Token 优化
python -m pytest tests/test_document_tasks_chunker.py -v  # 文档分块接入（含并行编排 + 知识图谱构建）
python -m pytest tests/test_vector_store.py -v            # 向量存储适配器
python -m pytest tests/test_audit_workflow.py -v          # 审核流程串联
python -m pytest tests/test_graph_service.py -v           # 知识图谱（三元组抽取 + chunk 计算复用）
python -m pytest tests/test_tool_guard.py -v              # MCP 工具调用守卫（deny-by-default + 确认管理 + engine 集成）
python -m pytest tests/test_citation_validation.py -v      # 引用强制校验（[n] 标注检测 + 来源映射 + 无引用拦截）
python -m pytest tests/test_skill_finder.py -v            # Find Skills 渐进式技能加载
python -m pytest tests/test_video_rag.py -v               # 视频 RAG（ASR + 关键帧 VLM 并发）
python -m pytest tests/test_document_parser.py -v         # 文档解析（Docling 统一解析 + PDF/DOCX/XLSX 表格+图片上传+小图过滤+VLM+扫描页OCR + 标题层级+列表结构+分页检测+XLSX降级+列宽对齐 + 独立音频 ASR + 旧格式兜底）
python -m pytest tests/test_migration.py -v               # Alembic 迁移 + Pydantic V2 配置校验（field_validator + model_validator + 迁移文件 + 端到端 SQLite）
python -m pytest tests/test_quality_guard.py -v           # RAG 质量守卫（检索+生成双层评估 + 忠实度拦截重生成 + 幻觉防护集成）
python -m pytest tests/test_rate_limiter.py -v            # API 限流（Redis-backed 令牌桶 + 内存降级 + Lua 原子化 + 客户端隔离 + FastAPI 集成）
python -m pytest tests/test_beat_lock.py -v               # Celery Beat 单实例锁（Redis SETNX + 分布式预备）
python -m pytest tests/test_eval.py -v                    # 离线评测（数据集+Recall/MRR/NDCG+回归基线+CLI）
python -m pytest tests/test_upload_summary.py -v          # P0 上传大小校验 + P1 解析摘要响应 + DB 字段优先读取
python -m pytest tests/test_model_fields_p0p2.py -v       # P0-P2 字段补全（tenant_id/AgentCheckpoint/stream_agent_response/UsageRecord/Subscription/Response Schema）
python -m pytest tests/test_security_bugfixes.py -v            # P0 安全与稳定性回归（SSE/熔断/锁/越权/IDOR）
python -m pytest tests/test_api_service_security_fixes.py -v   # P2 API/服务层回归（上传闸门/异步redis/脱敏/SSE连接释放）
python -m pytest tests/test_provider_circuit_breaker.py -v     # Provider 熔断器契约（失败开路/快速拒绝/恢复闭合）
python -m pytest tests/test_health_check.py -v                 # Provider 健康检查（并发检查/超时隔离/Redis 缓存）
python -m pytest tests/test_request_context.py -v              # P0 HTTP request_id contextvar
python -m pytest tests/test_tts_service.py -v                  # P1 TTS 语音合成（edge-tts）
python -m pytest tests/test_cross_modal.py -v                  # P2 跨模态检索（jina-clip-v2 文本+图片向量化）
python -m pytest tests/test_intent_router.py -v               # P1 意图路由（规则+LLM 混合意图识别）
python -m pytest tests/test_entity_registry.py -v             # P2 实体注册表（实体识别+查询扩展+本体谓词）
python -m pytest tests/test_focus_tracker.py -v               # P3-A 焦点追踪（TopicTracker + ConversationFocus）
python -m pytest tests/test_focus_tracker_enhanced.py -v      # P4-C 焦点历史栈增强
python -m pytest tests/test_context_selector.py -v            # P3-B 上下文选择器（语义相似度+时间衰减）
python -m pytest tests/test_conversation_summarizer.py -v     # P3-C 对话摘要（分层压缩）
python -m pytest tests/test_scratchpad.py -v                  # P3-E Scratchpad 草稿本
python -m pytest tests/test_fact_extraction.py -v             # P3-F LLM 事实提取
python -m pytest tests/test_drift_detector.py -v              # P4-A 漂移检测（三级策略）
python -m pytest tests/test_contradiction_detector.py -v      # P4-B 矛盾检测（三场景 + check_answer_consistency 接线）
python -m pytest tests/test_high_risk_detector.py -v          # 高风险信息核验（金额/日期/法律条款检测 + 来源一致性校验）
python -m pytest tests/test_preference_drift_detector.py -v   # P4-F 偏好偏移检测（纯规则）
python -m pytest tests/test_retrieval_matcher.py -v           # P4-D 检索匹配检测
python -m pytest tests/test_repetition_detector.py -v         # P4-G 重复提问检测
python -m pytest tests/test_coreference_enhanced.py -v        # P4-C 指代消解增强（历史+焦点栈注入）
python -m pytest tests/test_engine_focus_injection.py -v      # P4-E 焦点注入引擎
python -m pytest tests/test_p4_integration.py -v              # P4 集成测试（chat_service 集成）
python -m pytest tests/test_mem0_semantic_search.py -v       # Mem0 语义检索（cosine similarity + 关键词降级）
python -m pytest tests/test_crew_structured_comm.py -v       # 多 Agent 结构化通信（原始需求透传 + JSON 输出）
python -m pytest tests/test_key_decision_persistence.py -v   # 关键决策持久化（防中间遗忘）
python -m pytest tests/test_reviewer_agent.py -v             # ReviewerAgent 对抗审查（高风险操作审批/拒绝/降级）
python -m pytest tests/test_tool_governance.py -v            # 工具治理（描述负向边界 + Agent类型筛选 + 无匹配工具指令）
```

### 测试覆盖

| 测试文件 | 测试数 | 覆盖范围 |
|----------|--------|----------|
| `test_chunk_optimization.py` | 55 | Q&A 分块、内容类型路由、标题路径、Context Cliff、格式智能检测（Docling Markdown vs Legacy HTML） |
| `test_token_optimization.py` | 27 | CacheAligner、Prompt Caching、稳定 System Prompt、增量上下文 |
| `test_p1_token_optimization.py` | 32 | 跨轮去重、Reflect 摘要、L1 注入、历史窗口化 |
| `test_p2_token_optimization.py` | 35 | ContextBudgetManager、三段式压缩、引擎集成 |
| `test_document_tasks_chunker.py` | 56 | SemanticChunker 接入、索引元数据、端到端策略验证 |
| `test_vector_store.py` | 56 | VectorStoreBase 抽象、OpenSearch k-NN / Milvus 双后端、工厂、检索器集成 |
| `test_audit_workflow.py` | 19 | 文档审核流程串联、密级路由、AuditService.approve 触发发布 |
| `test_tool_guard.py` | 32 | DangerousToolGuard 守卫拦截、确认管理、**deny-by-default 未知工具阻断**、engine 集成 |
| `test_video_rag.py` | 37 | ASR Provider 工厂、视频处理器、视频转写分块、document_tasks 集成、关键帧 VLM 并发 |
| `test_document_parser.py` | 223 | Docling 统一解析、PDF 表格/图片上传/小图过滤/VLM/扫描页 OCR、PPTX GROUP 递归/图表数据/图片上传/小图过滤/列宽对齐/备注、DOCX 标题层级映射/列表结构/分页检测/图片上传/页眉页脚、XLSX 双引擎降级/列宽对齐、独立音频 ASR、旧格式兜底、factory 路由、document_tasks 集成、配置项 |
| `test_quality_guard.py` | 41 | 检索质量检查、重试决策、生成质量评估、低置信度标记、**忠实度拦截重生成（check_and_regenerate）**、**引用校验/矛盾检测/高风险核验集成**、engine 集成 |
| `test_citation_validation.py` | 12 | 引用标注提取、[n] 编号映射、**无引用标注强制拦截（validate_citations）**、未映射编号告警、空来源跳过、结果序列化 |
| `test_high_risk_detector.py` | 27 | 金额/日期/法律条款检测（正则匹配）、来源一致性核验、未核验比例阈值（>50% block / 部分 warn）、source_snippet 提取、结果序列化 |
| `test_rate_limiter.py` | 35 | 令牌桶消费/补充、客户端隔离、API Key/IP 标识、429 响应、健康检查豁免 |
| `test_eval.py` | 59 | 数据集加载、Recall@K/MRR/NDCG 计算、Runner 集成、回归检测、DB 持久化、CLI 退出码 |
| `test_skill_finder.py` | 58 | SkillMetadata 匹配分数、SkillRegistry 加载/索引/按需加载、SkillFinder 中英文匹配/阈值/fallback/max_skills、分词器、config 配置项、Server/MCPClient/Engine 集成 |
| `test_dashscope_provider.py` | 27 | DashScopeProvider 继承 VLLMProvider、初始化、chat/tool_use、DashScopeEmbedder 维度/embed、factory 路由、config 配置项、向后兼容性 |
| `test_minio_client.py` | 20 | MinIO upload/download/delete/exists、懒初始化、bucket 自动创建与缓存 |
| `test_migration.py` | 28 | Pydantic V2 field_validator（DATABASE_URL/数值/CORS）、model_validator（部署模式/SECRET_KEY）、迁移文件存在性/upgrade/downgrade、alembic env.py 配置、迁移 runner 端到端 PostgreSQL |
| `test_upload_summary.py` | 53 | P0 文件大小校验（MAX_UPLOAD_SIZE_MB 超限 413/MagicMock 回退）、P1 解析摘要响应（preview/structure/warnings/pages/char_count/parse_status）、结构标签提取、页数推断、解析状态推断、解析任务 warnings 收集、旧格式警告、认证强制、**DB 字段优先读取（page_count/char_count/parse_status/parse_warnings）**、**任务持久化解析元数据**、**迁移文件验证（4 字段 add_column/drop_column）** |
| `test_model_fields_p0p2.py` | 54 | P0-P2 字段补全：P0-1 tenant_id（KnowledgeBase/Document）、P0-2 MessageRepository limit 参数、P0-3 AgentCheckpoint ORM 模型、P0-4 stream_agent_response 异步生成器、P0-5 ApiKeyResponse expires_at/tenant_id、P1 DocResponse 8 字段、Notification.read_at DateTime 类型、10 模型 tenant_id、UsageRecord duration_ms/success/request_id、Subscription 6 字段补全、P2 Response Schema（7 个）+ updated_at（4 个）、迁移文件验证 |
| `test_testing_platform.py` | 64 | 智能测试平台：6 张表 ORM 模型、10 个枚举、5 个服务（需求提取/用例生成/评审/管理/编排）、28 个 API 端点、3 个 Celery 任务、JSON 解析 |
| `test_knowledge_compounding.py` | 47 | 知识回流层：3 张表 ORM 模型、Pydantic Schema、KnowledgeCompoundingService（5 步闭环：收集/提取/沉淀/冲突检测/复用注入）、JSON 解析、Celery 任务、API 路由注册 |
| 其他测试 | 454 | API 端点、服务层、模型层、记忆引擎、连接器、通知、图谱、多模态、租户门控、P0/P1/P2 适配器等 |
| 稳定性加固回归（`test_security_bugfixes.py` / `test_api_service_security_fixes.py` / `test_provider_circuit_breaker.py` / `test_health_check.py` / `test_beat_lock.py`） | 145 | P0-P3 修复回归：SSE 韧性、熔断器契约、任务锁、越权/IDOR、上传闸门、异步 redis、健康检查并发与超时、脱敏门控、**P2 全文索引字段名对齐 + kb_id 过滤** |
| `test_request_context.py` | 4 | P0 HTTP request_id contextvar（set/get/reset/嵌套） |
| `test_tts_service.py` | 7 | P1 TTS 语音合成（edge-tts 空文本/禁用/截断/合成/音色列表） |
| `test_cross_modal.py` | 22 | P2 跨模态检索（JinaCLIPEmbedder 文本+图片向量化、CrossModalService 入库、降级逻辑、维度一致性、C1/C2 独立索引隔离验证） |
| `test_intent_router.py` | 15 | P1 意图路由（规则匹配 7 种意图、LLM 兜底、快捷路径短路、优雅降级、集成测试） |
| `test_entity_registry.py` | 20 | P2 实体注册表（实体识别、查询扩展、本体谓词推理、EntityRegistry 集成） |
| `test_focus_tracker.py` | 27 | P3-A 焦点追踪（TopicTracker 规则+LLM 提取、ConversationFocus 序列化、置信度继承） |
| `test_focus_tracker_enhanced.py` | 9 | P4-C 焦点历史栈（栈大小限制、get_focus_history、reset_focus、多轮累积） |
| `test_context_selector.py` | 10 | P3-B 上下文选择器（语义相似度+时间衰减+去重、懒加载 Embedder） |
| `test_conversation_summarizer.py` | 8 | P3-C 对话摘要（分层压缩、关键信息保留、LLM 降级） |
| `test_scratchpad.py` | 5 | P3-E Scratchpad（草稿本累积、_think 注入、Token 截断） |
| `test_fact_extraction.py` | 15 | P3-F LLM 事实提取（偏好/决策/关键信息提取、Mem0 写入、降级） |
| `test_drift_detector.py` | 14 | P4-A 漂移检测（三级策略：规则/Embedding/置信度衰减、优雅降级） |
| `test_contradiction_detector.py` | 19 | P4-B 矛盾检测（用户陈述/回答-知识库/文档间三场景、共同实体预筛、LLM JSON 解析、**check_answer_consistency 接线引擎**） |
| `test_preference_drift_detector.py` | 16 | P4-F 偏好偏移检测（6 类偏好关键词、已有偏好去重、system prompt 修饰） |
| `test_retrieval_matcher.py` | 11 | P4-D 检索匹配检测（top-1 相似度、多文档、text 字段兼容、降级） |
| `test_repetition_detector.py` | 12 | P4-G 重复提问检测（cosine > 0.85、连续重复计数、current_embedding 复用） |
| `test_coreference_enhanced.py` | 11 | P4-C 指代消解增强（历史注入、焦点栈注入、截断、规则回退） |
| `test_engine_focus_injection.py` | 9 | P4-E 焦点注入引擎（AgentState 新字段、_think 动态注入、漂移警告） |
| `test_p4_integration.py` | 9 | P4 集成测试（PreparedChat P4 字段、SSE 事件类型、检测器协作、后台任务） |
| `test_mem0_semantic_search.py` | 23 | Mem0 语义检索（cosine similarity 向量排序、相似度阈值过滤、Embedder 不可用降级、关键词降级、无 embedding 兜底） |
| `test_crew_structured_comm.py` | 9 | 多 Agent 结构化通信（原始需求透传注入、JSON 输出指令、期望输出标记、空任务兜底、序列化降级） |
| `test_key_decision_persistence.py` | 7 | 关键决策持久化（决策关键词检测、LLM 提取、NONE 跳过、LLM 不可用降级、异常降级、TTL 24h） |
| `test_reviewer_agent.py` | 13 | ReviewerAgent 对抗审查（高风险工具判定、非高风险放行、LLM 不可用降级、审批/拒绝、异常降级、markdown JSON 解析、超长 query 截断） |
| `test_tool_governance.py` | 17 | 工具治理 P0-P2（描述负向边界约束、tags 口语化关键词、Agent 类型筛选 QA/Workflow/Action、未知类型安全默认、CrewAI 不可用降级、MCP 异常降级、无匹配工具指令构建） |
| **合计** | **3061** | **122 个测试文件，3061 项测试**（DB 连接相关用例依赖测试环境 PostgreSQL，未起服务时作为环境基线跳过） |

---