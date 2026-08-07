# 企业知识库大脑（Enterprise Knowledge Brain）

企业级知识库 SaaS 平台，基于 **Agentic RAG** 架构，面向多租户场景提供多模态文档处理、智能问答、协同编辑与知识图谱分析一体化能力。

## 核心功能

- **Agentic RAG 引擎**：`think → retrieve → tool_call → generate → reflect` 多轮 Agent Loop，RAG 核心层（分块/检索/重排/生成）自研实现，无外部框架依赖
- **多模态文档处理**：Docling 统一解析 PDF/DOCX/PPTX/XLSX/HTML/图片/音频 → 结构化 HTML，图片 VLM 描述、扫描件 OCR、视频 ASR 转写
- **双路混合检索**：Milvus 向量 + OpenSearch 全文召回 + Cohere Reranker 重排，四级语义分块 + 多级记忆（Short-term/Working/Long-term/Graph）
- **多租户隔离**：RLS 行级隔离 + 密级访问控制 + 租户级权限过滤
- **MCP 工具协议**：接入 OA/ERP/CRM 实时数据，DangerousToolGuard 守卫 + 工具审批流
- **实时对话智能**：意图路由、漂移检测、矛盾检测、指代消解、偏好识别等 P4 能力
- **智能测试平台**：测试计划/用例/执行/评审 + 知识回流层（知识复利闭环）
- **协同编辑**：Yjs + WebSocket CRDT 实时协同
- **跨模态检索**：jina-clip-v2 文本与图片向量独立索引，text-to-image 检索

## 技术栈

| 层级 | 技术选型 |
|------|----------|
| 后端 | FastAPI + SQLAlchemy(async) + Celery + Redis |
| 前端 | Astro 5 + React 19 + TypeScript（SSR + React Island，44 页面） |
| 数据库 | PostgreSQL 16 + pgvector、OpenSearch 2.18、Neo4j 5.26、Milvus 2.4（可选） |
| LLM | Anthropic Claude / DashScope 通义千问 / vLLM（Llama 3.3 / Qwen 3） |
| 文档解析 | Docling + pymupdf + python-pptx + python-docx + openpyxl + pandas |
| 部署 | Docker Compose + Caddy（自动 HTTPS + HTTP/3） |

支持 **SaaS（Claude）/ SaaS·国内（通义千问）/ 私有部署（vLLM + TEI）** 三种部署模式。

## 快速开始

```bash
# 克隆仓库
git clone <repo-url> && cd EnterpriseKnowledge

# 配置环境变量
cp backend/.env.example backend/.env
# 编辑 .env 设置 ANTHROPIC_API_KEY、DATABASE_URL 等

# 启动全部服务
docker-compose up -d
```

详细部署配置见 [部署指南](docs/deployment.md)。

## 文档

设计思想与架构细节详见 `docs/` 目录：

| 文档 | 内容 |
|------|------|
| [系统架构与 Agent Loop](docs/architecture.md) | 架构设计、Agent Loop、MCP 守卫、RAG 质量守卫 |
| [RAG 检索与记忆架构](docs/rag.md) | 四级语义分块、混合检索管线、四级记忆 |
| [Token 优化与上下文压缩](docs/token-optimization.md) | 14 个 Token 浪费点、六级优化方案 |
| [核心功能设计](docs/features.md) | 门控、通知、Yjs 协同、SSE、工具审批、LLM Provider、限流等 |
| [智能测试平台与离线评测](docs/testing-platform.md) | 智能测试平台、知识回流层、离线评测系统 |
| [稳定性与安全加固](docs/security.md) | P0-P3 安全与稳定性修复 |
| [对话智能与多 Agent 协作](docs/intelligence.md) | 意图路由、实体注册表、上下文工程、P4 实时对话智能、多 Agent 协作、工具治理 |
| [项目结构](docs/project-structure.md) | 完整目录树 |
| [测试](docs/testing.md) | 测试运行方式与覆盖 |

## License

Private - All Rights Reserved