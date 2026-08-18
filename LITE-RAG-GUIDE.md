# LiteRAG — 从零搭建生产级 RAG 知识库

> 8 天跑通，30 天上线。不是 Demo，是真能用的东西。

---

## 这个项目是什么

LiteRAG 是 [EnterpriseKnowledge](https://github.com/jamesfeng2009/KnowledgeBase) 完整项目的精简开源版。完整版包含 20 个后端模块、28 个服务类、271 个源文件、2296 个测试用例，覆盖了多租户隔离、知识图谱、多模态、Agent Loop、记忆引擎等全套企业级能力——但对初学者来说太重了。

LiteRAG 保留了让 RAG "真正好用"的核心能力，砍掉了所有工程化噪音，让你在 8 天内理解每一行代码的用途。

**保留什么**：语义分块 + 父子索引、混合检索（向量 + 全文）、重排、流式生成 + 引用标注、文档解析（PDF / DOCX / MD）、Agent Loop（简化版）、记忆引擎（简化版）、上下文工程（简化版）、FastAPI 接口、Docker 一键部署。

**砍掉什么**：多租户、知识图谱、多模态、视频 RAG、MCP 工具协议、CrewAI 多 Agent 协作、熔断器、Celery。

Agent Loop、记忆引擎、上下文工程是智能体的三大核心组件，功能可以阉割但组件不能缺失——LiteRAG 保留了它们的骨架设计，让你理解智能体如何"思考、记忆、理解上下文"。

---

## 目录

- [适合谁学](#适合谁学)
- [学完你能做什么](#学完你能做什么)
- [技术栈一览](#技术栈一览)
- [架构设计](#架构设计)
- [项目目录结构](#项目目录结构)
- [模块详解](#模块详解)
  - [1. 文档解析层](#1-文档解析层)
  - [2. 语义分块层](#2-语义分块层)
  - [3. 向量化层](#3-向量化层)
  - [4. 检索层](#4-检索层)
  - [5. 重排层](#5-重排层)
  - [6. 生成层](#6-生成层)
  - [7. API 层](#7-api-层)
  - [8. Agent Loop 层](#8-agent-loop-层)
  - [9. 记忆引擎层](#9-记忆引擎层)
  - [10. 上下文工程层](#10-上下文工程层)
- [数据库设计](#数据库设计)
- [部署方案](#部署方案)
- [8 天学习路径](#8-天学习路径)
- [常见坑点](#常见坑点)
- [测试指南](#测试指南)
- [与完整版的差异](#与完整版的差异)
- [进阶路线图](#进阶路线图)
- [FAQ](#faq)
- [开源协议与课程说明](#开源协议与课程说明)

---

## 适合谁学

| 人群 | 前置要求 | 学完收益 |
|------|---------|---------|
| Python 后端工程师 | 会 async/await，用过 FastAPI 或 Flask | 掌握 RAG 全链路实现，能独立交付知识库项目 |
| AI 应用开发者 | 调过 OpenAI API，了解 Embedding 概念 | 理解检索增强生成的工程细节，不再依赖 LangChain 黑盒 |
| 技术管理者 / 架构师 | 理解微服务架构，能读懂 Python | 评估 RAG 技术选型，制定团队技术方案 |
| 数据工程师 | 熟悉 ETL，了解向量数据库概念 | 把 RAG 纳入数据管线，构建企业知识中枢 |
| 在校学生 / 求职者 | Python 基础，做过 Web 项目 | 简历上多一个"从零搭建生产级 RAG"的实战项目 |

**不需要**的前提：不需要机器学习背景，不需要懂 Transformer 原理，不需要用过 LangChain / LlamaIndex。所有概念都会从零讲起。

---

## 学完你能做什么

1. **从零搭建一个能用的 RAG 系统** — 不是跑通别人的 Demo，而是自己写每一行核心代码
2. **理解 RAG 每个环节为什么这样设计** — 不是调库，是知道分块为什么 256 token、检索为什么两路、重排为什么需要
3. **掌握智能体三大核心组件** — Agent Loop（思考→执行→反思循环）、记忆引擎（跨轮/跨会话记忆）、上下文工程（焦点追踪 + 指代消解），理解普通 RAG 和 Agentic RAG 的本质区别
4. **根据业务调优检索质量** — 知道调哪些参数、怎么评估效果、怎么定位问题
5. **替换任意组件** — 换 Embedding 模型、换向量数据库、换 LLM，代码零修改
6. **评估开源 RAG 平台** — 看 Dify / FastGPT / RAGFlow 的源码时，知道它们在做什么、哪里做得好、哪里可以改
7. **继续深入进阶方向** — 知识图谱、多模态、多 Agent 协作、MCP 工具协议，有清晰的进阶路径

---

## 技术栈一览

### 后端

| 分类 | 技术 | 版本 | 为什么选它 |
|------|------|------|-----------|
| Web 框架 | FastAPI | ≥0.115 | 异步原生、自动 OpenAPI 文档、类型安全 |
| 数据库 | PostgreSQL | 16 | JSONB 支持、成熟稳定、pgvector 扩展 |
| ORM | SQLAlchemy[asyncio] | ≥2.0.36 | 异步原生、Python 生态最成熟的 ORM |
| 全文 + 向量 | OpenSearch | 2.18 | BM25 全文检索 + k-NN 向量检索共用一个引擎，运维简单 |
| 缓存 | Redis | 7 | 令牌桶限流、进度追踪、可选缓存 |
| LLM SDK | openai | ≥1.50 | 兼容 OpenAI API 格式，可接 Claude / vLLM / DashScope |
| 文档解析 | docling | ≥2.8 | IBM 开源，AI 驱动版面分析，统一输出 HTML |
| Agent 编排 | langgraph | ≥0.2 | Agent Loop 状态图编排，2025 年 v1.0 LTS，生产级 Agent 事实标准 |
| 配置 | pydantic-settings | ≥2.6 | 类型安全的配置管理，环境变量覆盖 |
| 日志 | structlog | ≥24.4 | 结构化日志，JSON 输出方便 ELK 采集 |
| HTTP 客户端 | httpx | ≥0.27 | 异步原生，连接池复用，支持 HTTP/2 |

### 前端（可选，课程不含）

完整版使用 Astro 5 + React 19 + TypeScript。LiteRAG 建议直接用 FastAPI 自带的 Swagger UI（`/docs`）调试 API，或用 Streamlit / Gradio 快速搭一个聊天界面。

### 基础设施

| 服务 | 用途 | 端口 | 内存占用 |
|------|------|------|---------|
| PostgreSQL 16 | 文档元数据、知识库、用户 | 5432 | ~100MB |
| Redis 7 | 限流、进度追踪 | 6379 | ~30MB |
| OpenSearch 2.18 | 全文检索 + 向量检索 | 9200 | ~1GB |

三个容器，`docker compose up -d` 一键启动。开发机 8GB 内存够用。

### 为什么用 LangGraph 但不用 LangChain

LangChain 和 LangGraph 看起来同属 LangChain 生态，但定位完全不同。LangChain 是**框架**——它的 `RetrievalQA.from_chain_type(...)` 把检索、重排、生成全藏起来了，你写的是配置，看不到细节。LangGraph 是**运行时**——它只管状态图的编排（节点、边、条件路由），不碰你的业务逻辑。用 LangGraph 编排 Agent Loop，你的 chunker、retriever、reranker、generator 仍然是手写的，每一行都看得见。

LangGraph v1.0 LTS 于 2025 年 10 月正式发布 [$TRAE_REF](http://m.toutiao.com/group/7658595970690662962/)，LinkedIn、Uber、Klarna、J.P. Morgan 都在生产环境使用 [$TRAE_REF](http://m.toutiao.com/group/7658595970690662962/)。与此同时，LangChain 的经典 `AgentExecutor` 已被官方弃用 [$TRAE_REF](http://m.toutiao.com/group/7658595970690662962/)，进入维护模式，官方推荐所有新项目迁移到 LangGraph。

简单说：**LangGraph 只管"怎么循环"，不管"循环里做什么"**。检索、重排、生成的逻辑仍然是你的手写代码，只是作为 LangGraph 的节点函数被调用。这和 LiteRAG"理解每一行代码"的目标完全一致。

---

## 架构设计

### 整体数据流

```
用户提问 "报销流程是什么？"
    │
    ▼
┌─────────────────────────────────────────────────────┐
│  Agent Loop (think → execute → reflect)             │
│                                                     │
│  ┌─ think: 决策（简化版直接进入 execute）            │
│  │                                                   │
│  │  ┌─ execute: 核心执行阶段 ──────────────────┐    │
│  │  │                                           │    │
│  │  │  1. 记忆引擎加载上下文                     │    │
│  │  │     ├── L1 短期窗口（最近 20 条对话）       │    │
│  │  │     └── L2 用户偏好（PostgreSQL）          │    │
│  │  │                                           │    │
│  │  │  2. 上下文工程预处理                       │    │
│  │  │     ├── 焦点追踪（提取话题/实体/意图）      │    │
│  │  │     └── 指代消解（补全省略句）              │    │
│  │  │                                           │    │
│  │  │  3. 混合检索器 (HybridRetriever)           │    │
│  │  │     ├── 向量检索 (k-NN cosine)            │    │
│  │  │     ├── 全文检索 (BM25)                   │    │
│  │  │     └── 合并 + 去重 + 父块回溯             │    │
│  │  │           ↓                                │    │
│  │  │     重排器 (Cohere API)                   │    │
│  │  │           ↓                                │    │
│  │  │  4. 生成器 (Generator)                     │    │
│  │  │     ├── 组装上下文 prompt                  │    │
│  │  │     ├── 调用 LLM 流式生成                  │    │
│  │  │     └── 提取 [1][2] 引用标注              │    │
│  │  │           ↓                                │    │
│  │  │     SSE 逐 token 返回前端                 │    │
│  │  └───────────────────────────────────────────┘    │
│  │                                                   │
│  └─ reflect: 反思（答案太短？重试，最多 5 轮）       │
│                                                     │
│  循环结束后: 记忆引擎保存对话摘要 + 提取用户偏好      │
└─────────────────────────────────────────────────────┘
```

### 分层架构

```
┌─────────────────────────────────────────────────────┐
│                    API 层 (FastAPI)                  │
│         /documents  /chat  /search  /knowledge      │
├─────────────────────────────────────────────────────┤
│                  Agent 层（LangGraph StateGraph）      │
│  ┌──────────────────────────────────────────────┐   │
│  │  Agent Loop (think→execute→reflect)         │   │
│  │  ├── 记忆引擎 (L1 短期 + L2 偏好)            │   │
│  │  └── 上下文工程 (焦点追踪 + 指代消解)          │   │
│  └──────────────────────────────────────────────┘   │
├─────────────────────────────────────────────────────┤
│                   Service 层                        │
│    KnowledgeService  ChatService  SearchService     │
├─────────────────────────────────────────────────────┤
│                    RAG 层                            │
│  Chunker → Retriever → Reranker → Generator         │
├─────────────────────────────────────────────────────┤
│                基础设施层                             │
│  Embedder  VectorStore  DocumentParser  LLMProvider │
├─────────────────────────────────────────────────────┤
│                  存储层                              │
│     PostgreSQL     OpenSearch     Redis             │
└─────────────────────────────────────────────────────┘
```

每一层都通过抽象基类 + 工厂函数解耦。Agent 层是智能体的核心——Agent Loop 编排"思考→执行→反思"循环，记忆引擎提供跨轮/跨会话上下文，上下文工程让系统理解省略句和指代。RAG 层的检索、重排、生成作为 Agent Loop 的 execute 阶段的执行单元被调用。你不需要理解全部代码就能替换某个组件——比如把 OpenAI Embedder 换成本地 BGE-M3，只需注册一个新的工厂函数，调用方代码零修改。

### 核心设计原则

| 原则 | 在代码中的体现 | 好处 |
|------|--------------|------|
| 单一职责 | 每个模块只做一件事：chunker 只分块，retriever 只检索 | 改一处不影响其他 |
| 开闭原则 | 工厂注册表模式，新增组件只注册不改旧代码 | 扩展不破坏 |
| 依赖倒置 | 调用方面向抽象基类，不感知具体实现 | 可测试可替换 |
| 优雅降级 | 外部依赖挂了返回空结果，不抛 500 | 系统韧性 |

---

## 项目目录结构

这是你需要创建的完整目录树。每个文件的作用在后文模块详解中说明。

```
LiteRAG/
├── backend/
│   ├── app/
│   │   ├── __init__.py
│   │   ├── main.py                    # FastAPI 入口，挂载路由
│   │   ├── config.py                  # Pydantic Settings 配置管理
│   │   ├── database.py                # SQLAlchemy 异步引擎 + Session
│   │   ├── deps.py                    # FastAPI 依赖注入
│   │   │
│   │   ├── document/                  # 文档解析层
│   │   │   ├── __init__.py
│   │   │   ├── base.py                # DocumentParser 抽象基类
│   │   │   ├── factory.py             # get_parser(doc_type) 工厂
│   │   │   ├── docling_parser.py      # Docling 统一解析器
│   │   │   ├── markdown_parser.py     # Markdown 解析器（零依赖）
│   │   │   ├── pdf_parser.py           # PDF 降级解析器（pymupdf）
│   │   │   └── docx_parser.py          # DOCX 降级解析器（python-docx）
│   │   │
│   │   ├── rag/                       # RAG 核心层
│   │   │   ├── __init__.py
│   │   │   ├── chunker.py            # 语义分块 + 父子索引
│   │   │   ├── retriever.py          # 混合检索（向量 + 全文）
│   │   │   ├── reranker.py            # 重排器（Cohere / 跳过）
│   │   │   ├── generator.py          # 流式生成 + 上下文组装
│   │   │   ├── citation.py           # 引用标注提取
│   │   │   └── vector_store/
│   │   │       ├── __init__.py
│   │   │       ├── base.py            # VectorStoreBase 抽象
│   │   │       ├── opensearch_store.py # OpenSearch k-NN 实现
│   │   │       └── factory.py          # get_vector_store() 工厂
│   │   │
│   │   ├── llm/                      # LLM 调用层
│   │   │   ├── __init__.py
│   │   │   ├── base.py                # LLMProvider / EmbeddingProvider 抽象
│   │   │   ├── embedder.py            # OpenAI Embedder 实现
│   │   │   ├── factory.py             # get_llm() / get_embedder() 工厂
│   │   │   └── openai_provider.py    # OpenAI Chat 实现
│   │   │
│   │   ├── models/                    # SQLAlchemy 数据模型
│   │   │   ├── __init__.py
│   │   │   ├── base.py                # Declarative Base
│   │   │   ├── knowledge.py           # KnowledgeBase 模型
│   │   │   └── document.py            # Document 模型
│   │   │
│   │   ├── schemas/                   # Pydantic 请求/响应模型
│   │   │   ├── __init__.py
│   │   │   ├── knowledge.py
│   │   │   ├── document.py
│   │   │   ├── chat.py
│   │   │   └── search.py
│   │   │
│   │   ├── api/v1/                   # API 路由层
│   │   │   ├── __init__.py
│   │   │   ├── knowledge.py          # 知识库 CRUD
│   │   │   ├── documents.py          # 文档上传 + 处理
│   │   │   ├── search.py             # 搜索接口
│   │   │   └── chat.py               # 流式对话
│   │   │
│   │   ├── agents/                    # Agent Loop 层（智能体核心）
│   │   │   ├── __init__.py            # 导出 + 触发自动注册
│   │   │   ├── base.py                # BaseAgent 主循环（AgentState 定义在 state.py）
│   │   │   ├── state.py               # AgentState 唯一权威定义（engine 与 base 共享）
│   │   │   ├── qa_agent.py            # QAAgent — Agentic RAG 问答
│   │   │   └── registry.py            # AgentRegistry 注册表
│   │   │
│   │   ├── memory/                    # 记忆引擎层（智能体核心）
│   │   │   ├── __init__.py            # 导出 MemoryManager + MemoryContext
│   │   │   ├── memory_context.py      # MemoryContext 数据类
│   │   │   └── memory_manager.py      # MemoryManager 编排器（L1 短期 + L2 偏好）
│   │   │
│   │   ├── context/                   # 上下文工程层（智能体核心）
│   │   │   ├── __init__.py            # 导出 TopicTracker + CoreferenceResolver
│   │   │   ├── focus_tracker.py       # 焦点追踪器（规则版）
│   │   │   └── coreference_resolver.py # 指代消解器（规则版）
│   │   │
│   │   ├── services/                  # 业务服务层
│   │   │   ├── __init__.py
│   │   │   ├── knowledge_service.py
│   │   │   ├── chat_service.py
│   │   │   └── search_service.py
│   │   │
│   │   └── utils/                    # 工具层
│   │       ├── __init__.py
│   │       ├── logger.py             # structlog 日志封装
│   │       └── sse.py                # SSE 响应工具
│   │
│   ├── tests/                        # 测试
│   │   ├── __init__.py
│   │   ├── conftest.py               # pytest fixtures
│   │   ├── test_chunker.py
│   │   ├── test_retriever.py
│   │   ├── test_generator.py
│   │   ├── test_agent.py             # Agent Loop 测试
│   │   ├── test_memory.py            # 记忆引擎测试
│   │   ├── test_context.py           # 上下文工程测试
│   │   └── test_api.py
│   │
│   ├── requirements.txt
│   ├── Dockerfile
│   └── .env.example
│
├── docker-compose.yml                # 一键启动三件套
├── .env                              # 环境变量
└── README.md
```

总共约 40 个源文件，核心逻辑集中在 `document/`、`rag/`、`llm/`、`agents/`、`memory/`、`context/` 六个目录。其中 `agents/`、`memory/`、`context/` 是智能体三大核心组件，功能简化但架构完整。每个文件 100-300 行，没有超过 500 行的文件。

---

## 模块详解

### 1. 文档解析层

**目标**：把 PDF、DOCX、Markdown 文件解析成纯文本或 HTML，交给分块器处理。

**核心文件**：`app/document/`

#### 1.1 解析器抽象

`base.py` 定义统一接口，所有解析器都继承它：

```python
from abc import ABC, abstractmethod


class DocumentParser(ABC):
    """文档解析器抽象基类。"""

    @abstractmethod
    async def parse(self, file_path: str) -> str:
        """解析文档，返回纯文本或 HTML。

        Args:
            file_path: 文件本地路径。

        Returns:
            解析后的文本内容。
        """
        ...
```

#### 1.2 解析器工厂

`factory.py` 按文档类型分发解析器。使用注册表模式，新增格式只需注册映射：

```python
from app.document.base import DocumentParser

# 文档类型 → 解析器类
_PARSER_CLASSES: dict[str, type[DocumentParser]] = {}


def _register_parsers() -> None:
    """注册所有文档解析器。"""
    from app.document.docx_parser import DOCXParser
    from app.document.markdown_parser import MarkdownParser
    from app.document.pdf_parser import PDFParser

    _PARSER_CLASSES["pdf"] = PDFParser
    _PARSER_CLASSES["docx"] = DOCXParser
    _PARSER_CLASSES["md"] = MarkdownParser
    _PARSER_CLASSES["markdown"] = MarkdownParser


def get_parser(doc_type: str) -> DocumentParser | None:
    """获取文档解析器。

    优先返回 DoclingParser（如果已安装且启用），
    降级返回原有专用解析器。

    Args:
        doc_type: 文档类型（pdf / docx / md）。

    Returns:
        解析器实例。不支持时返回 None。
    """
    if not _PARSER_CLASSES:
        _register_parsers()

    # 1. 优先尝试 Docling 统一解析器
    if _is_docling_enabled():
        from app.document.docling_parser import DoclingParser
        if DoclingParser.is_supported(doc_type.lower()):
            return DoclingParser()

    # 2. 降级到专用解析器
    cls = _PARSER_CLASSES.get(doc_type.lower())
    return cls() if cls else None
```

| 格式 | 优先解析器 | 降级解析器 | 依赖库 |
|------|-----------|-----------|--------|
| PDF | Docling（AI 版面分析） | pymupdf | pymupdf ≥1.24 |
| DOCX | Docling | python-docx | python-docx ≥1.1 |
| MD | MarkdownParser（零依赖） | — | 无 |

#### 1.3 MarkdownParser 实现

最简单的解析器，零依赖，适合作为第一个练手的解析器：

```python
import pathlib

from app.document.base import DocumentParser


class MarkdownParser(DocumentParser):
    """Markdown 解析器 — 直接读取文件内容，零依赖。"""

    async def parse(self, file_path: str) -> str:
        text = pathlib.Path(file_path).read_text(encoding="utf-8")
        return text
```

#### 1.4 PDFParser 降级实现

用 pymupdf 读取 PDF 文本。能处理基础 PDF，复杂版面（多栏、表格）效果不如 Docling：

```python
import fitz  # pymupdf

from app.document.base import DocumentParser


class PDFParser(DocumentParser):
    """PDF 解析器 — pymupdf 降级路径。"""

    async def parse(self, file_path: str) -> str:
        doc = fitz.open(file_path)
        parts = []
        for page in doc:
            parts.append(page.get_text("text"))
        doc.close()
        return "\n\n".join(parts)
```

#### 1.5 Docling 统一解析器（可选）

Docling 是 IBM 开源的文档解析库，基于 Granite-Docling-258M 模型，能处理多栏布局、无边框表格、扫描件 OCR。它把所有格式统一输出为 HTML，后续处理逻辑只需面向一种格式。

```python
from app.document.base import DocumentParser


class DoclingParser(DocumentParser):
    """Docling 统一解析器 — AI 驱动版面分析。"""

    @staticmethod
    def is_available() -> bool:
        try:
            import docling  # noqa: F401
            return True
        except ImportError:
            return False

    @staticmethod
    def is_supported(doc_type: str) -> bool:
        return doc_type in {"pdf", "docx", "pptx", "xlsx", "html"}

    async def parse(self, file_path: str) -> str:
        from docling.document_converter import DocumentConverter

        converter = DocumentConverter()
        result = converter.convert(file_path)
        # 统一输出 HTML，保留表格和标题结构
        return result.document.export_to_markdown()
```

#### 1.6 为什么不用 LangChain 的文档加载器

LangChain 的 `PyPDFLoader`、`UnstructuredFileLoader` 适合快速原型，但三个问题让它们不适合生产：

1. **表格丢失** — PyPDFLoader 把表格拆成散乱文本，丢失行列结构
2. **阅读顺序错乱** — 多栏 PDF 按物理位置读取，左右栏交错
3. **图片被忽略** — 图片内的文字和图表信息全部丢弃

Docling 用 AI 模型理解版面布局，能正确还原阅读顺序和表格结构。如果不想装 Docling，pymupdf 降级路径也能处理基础 PDF。

---

### 2. 语义分块层

**目标**：把长文档切成语义连贯的小块（Chunk），每块是一个独立的知识单元，既用于向量检索，也作为 LLM 上下文。

**核心文件**：`app/rag/chunker.py`

#### 2.1 Chunk 数据结构

```python
from dataclasses import dataclass


@dataclass(frozen=True)
class Chunk:
    """文档分块 — 检索与上下文注入的最小单元。"""

    id: str              # 分块唯一标识（UUID）
    doc_id: str          # 所属文档 ID
    content: str         # 分块文本内容
    parent_id: str | None  # 父块 ID（父子索引模式）；无父块时为 None
    start_pos: int       # 原文起始字符偏移
    end_pos: int         # 原文结束字符偏移
    token_count: int     # 估算 token 数
    title_path: str      # 标题路径锚点（如 "Redis > 集群 > 哈希槽"）
    content_type: str    # 内容类型标签
    chunk_strategy: str  # 实际使用的策略名（structural / semantic / fallback）
```

`token_count` 使用 `字符数 / 3.5` 估算，不依赖 tiktoken，零额外依赖：

```python
import math

_CHARS_PER_TOKEN: float = 3.5

def estimate_tokens(text: str) -> int:
    """估算文本 token 数（中英文混合启发式）。"""
    if not text:
        return 0
    return max(1, math.ceil(len(text) / _CHARS_PER_TOKEN))
```

#### 2.2 三种分块策略

LiteRAG 实现三种分块策略，按优先级自动选择：

| 优先级 | 策略 | 触发条件 | 算法 |
|--------|------|---------|------|
| 1 | 结构化分块 | Markdown / HTML 文档 | 按 `#` 标题分割，提取标题路径 |
| 2 | 语义分块（TextTiling） | 纯文本文档 | 滑动窗口计算相邻段落 Jaccard 相似度，在谷底分割 |
| 3 | 固定长度兜底 | 以上均无效 | 512 token 硬切 |

**结构化分块** 保留标题层级信息。比如文档有 `# Redis` → `## 集群` → `### 哈希槽`，分块后每个 chunk 会带上 `title_path="Redis > 集群 > 哈希槽"`，拼入 content 前缀增强 embedding 上下文感知。

**语义分块** 使用 TextTiling 算法。它不按固定长度切，而是分析文本的"话题边界"：用 3 句滑动窗口计算相邻段落的词重叠度（Jaccard 相似度），在相似度谷底处分割。这保证了每个 chunk 内部话题连贯，chunk 之间有明确的话题切换。

**固定长度兜底** 是最后的保险。512 token 硬切，可选 175 字符 Overlap 重叠（默认关闭）。

#### 2.3 分块入口

```python
# 父子索引参数
_CHILD_TOKENS: int = 256
_PARENT_TOKENS: int = 1024
# 固定长度兜底参数
_FALLBACK_TOKENS: int = 512
# 结构化分块产出有效性的最小块数
_MIN_STRUCTURAL_CHUNKS: int = 2


def chunk_document(
    text: str,
    doc_type: str = "md",
    doc_id: str = "",
    content_type: str = "auto",
) -> list[Chunk]:
    """文档分块入口 — 按优先级选择策略。

    Args:
        text: 文档纯文本或 HTML。
        doc_type: 文档类型。
        doc_id: 文档 ID。
        content_type: 内容类型（auto / faq / tutorial / specification / report / plain）。

    Returns:
        Chunk 列表，已构建父子索引。
    """
    # 1. 结构化分块（Markdown / HTML 标题）
    chunks = _chunk_structural(text, doc_id)
    if len(chunks) >= _MIN_STRUCTURAL_CHUNKS:
        return _build_parent_child_index(chunks)

    # 2. 语义分块（TextTiling）
    chunks = _chunk_semantic(text, doc_id)
    if chunks:
        return _build_parent_child_index(chunks)

    # 3. 固定长度兜底
    return _chunk_fixed_length(text, doc_id)
```

#### 2.4 父子索引 — 让 RAG 质量翻倍的关键

这是 LiteRAG 区别于市面"切片 + 向量库"课程的核心设计。

**问题**：语义分块产出的小块（256 token）向量检索精准，但交给 LLM 时上下文太短，模型无法理解完整语境。

**解法**：双层分块。小块用于检索（精准命中），大块用于生成（完整上下文）。

```
原文档
┌─────────────────────────────────────────────────┐
│  # 报销流程                                       │
│  ## 提交申请                                      │
│  员工登录OA系统，选择「费用报销」...  ← 子块A (256t) │
│  填写报销单据，附上发票照片...      ← 子块B (256t) │
│  ## 审批流程                                      │
│  提交后自动流转到直属上级...        ← 子块C (256t) │
│  上级审批通过后转财务...           ← 子块D (256t) │
└─────────────────────────────────────────────────┘
         │ 父块 (1024 token)
         ▼
┌─────────────────────────────────────────────────┐
│  父块1: 子块A + 子块B (报销申请完整上下文)          │
│  父块2: 子块C + 子块D (审批流程完整上下文)          │
└─────────────────────────────────────────────────┘
```

**检索流程**：
1. 用子块做向量检索（精准命中）
2. 通过 `parent_id` 回取父块原文（提供完整上下文）
3. 用父块内容替换子块内容，交给 LLM 生成

父子索引构建逻辑：

```python
def _build_parent_child_index(chunks: list[Chunk]) -> list[Chunk]:
    """将连续的子块组合为父块，建立 parent_id 关联。

    子块: 256 token（用于向量检索，精准命中）
    父块: 1024 token（用于 LLM 上下文，提供完整语境）

    每 4 个连续子块合并为 1 个父块。
    """
    if not chunks:
        return chunks

    result: list[Chunk] = []
    i = 0
    while i < len(chunks):
        # 取 4 个子块组成一个父块
        group = chunks[i : i + 4]
        parent_content = "\n\n".join(c.content for c in group)

        # 创建父块（也作为向量检索候选）
        parent = Chunk(
            id=str(uuid.uuid4()),
            doc_id=group[0].doc_id,
            content=parent_content,
            parent_id=None,  # 父块没有父块
            start_pos=group[0].start_pos,
            end_pos=group[-1].end_pos,
            token_count=estimate_tokens(parent_content),
            title_path=group[0].title_path,
            content_type=group[0].content_type,
            chunk_strategy=group[0].chunk_strategy,
        )
        result.append(parent)

        # 子块指向父块
        for child in group:
            result.append(
                replace(child, parent_id=parent.id)
            )

        i += 4

    return result
```

#### 2.5 TextTiling 语义分块实现

TextTiling 算法的核心思想：用滑动窗口计算相邻段落的词汇相似度，在相似度"谷底"（话题切换点）处分割。

```python
import re

# 滑动窗口大小（按句/段计）
_SEMANTIC_WINDOW: int = 3


def _chunk_semantic(text: str, doc_id: str) -> list[Chunk]:
    """TextTiling 语义分块 — 在话题边界处分割。

    算法步骤：
    1. 将文本按段落分割
    2. 用滑动窗口计算相邻段落的词重叠度（Jaccard 相似度）
    3. 在相似度谷底处分割
    4. 每个段落块不超过 _CHILD_TOKENS
    """
    paragraphs = _split_paragraphs(text)
    if len(paragraphs) < 4:
        # 文本太短，不值得语义分块
        return []

    # 计算相邻段落相似度序列
    similarities = _compute_similarity_sequence(paragraphs)

    # 找到相似度谷底作为分割点
    boundaries = _find_valleys(similarities)

    # 按分割点组装 chunk
    chunks: list[Chunk] = []
    start = 0
    for boundary in boundaries:
        chunk_text = "\n\n".join(paragraphs[start:boundary])
        if estimate_tokens(chunk_text) <= _CHILD_TOKENS:
            chunks.append(_make_chunk(chunk_text, doc_id, start, boundary))
        else:
            # 超过子块大小，继续切分
            sub = _chunk_fixed_length(chunk_text, doc_id)
            chunks.extend(sub)
        start = boundary

    # 最后一段
    if start < len(paragraphs):
        chunk_text = "\n\n".join(paragraphs[start:])
        chunks.append(_make_chunk(chunk_text, doc_id, start, len(paragraphs)))

    return chunks


def _compute_similarity_sequence(paragraphs: list[str]) -> list[float]:
    """计算相邻段落的 Jaccard 相似度。"""
    similarities = []
    for i in range(len(paragraphs) - 1):
        words_a = set(_tokenize(paragraphs[i]))
        words_b = set(_tokenize(paragraphs[i + 1]))
        if not words_a and not words_b:
            similarities.append(1.0)
        elif not words_a or not words_b:
            similarities.append(0.0)
        else:
            # Jaccard 相似度 = 交集 / 并集
            intersection = words_a & words_b
            union = words_a | words_b
            similarities.append(len(intersection) / len(union))
    return similarities


def _find_valleys(similarities: list[float]) -> list[int]:
    """找到相似度序列的谷底位置作为分割点。"""
    if len(similarities) < 3:
        return [len(similarities)]

    valleys = []
    threshold = sum(similarities) / len(similarities)
    for i in range(1, len(similarities) - 1):
        # 谷底：局部最小值且低于平均值
        if (
            similarities[i] < similarities[i - 1]
            and similarities[i] < similarities[i + 1]
            and similarities[i] < threshold
        ):
            valleys.append(i + 1)  # +1 因为相似度索引对应段落边界

    if not valleys:
        # 没有找到谷底，在中间分割
        mid = len(similarities) // 2
        valleys = [mid]

    valleys.append(len(similarities))
    return valleys


def _tokenize(text: str) -> list[str]:
    """简单分词 — 按空格和标点分割，小写化。"""
    return re.findall(r"\w+", text.lower())
```

---

### 3. 向量化层

**目标**：把文本块转为向量，写入 OpenSearch 供检索。

**核心文件**：`app/llm/embedder.py`、`app/rag/vector_store/`

#### 3.1 Embedder 抽象

```python
from abc import ABC, abstractmethod


class EmbeddingProvider(ABC):
    """Embedding 统一接口。"""

    #: 输出向量维度，子类覆盖（用于建库时确定 collection dim）
    dim: int = 0

    @abstractmethod
    async def embed(self, texts: list[str]) -> list[list[float]]:
        """批量向量化。

        Args:
            texts: 文本列表。

        Returns:
            向量列表，与入参等长。
        """
        ...
```

#### 3.2 OpenAI Embedder 实现

```python
from openai import AsyncOpenAI

from app.config import get_settings
from app.llm.base import EmbeddingProvider


class OpenAIEmbedder(EmbeddingProvider):
    """OpenAI text-embedding-3-large（3072 维）。"""

    dim = 3072

    def __init__(self) -> None:
        settings = get_settings()
        self.client = AsyncOpenAI(api_key=settings.OPENAI_API_KEY)
        self.model = "text-embedding-3-large"

    async def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        # OpenAI API 单次最多 2048 条
        batch_size = 2048
        all_embeddings = []
        for i in range(0, len(texts), batch_size):
            batch = texts[i : i + batch_size]
            resp = await self.client.embeddings.create(
                model=self.model,
                input=batch,
            )
            all_embeddings.extend([d.embedding for d in resp.data])
        return all_embeddings
```

#### 3.3 向量存储抽象

```python
from abc import ABC, abstractmethod
from typing import Any


class VectorStoreBase(ABC):
    """向量存储统一接口。"""

    @abstractmethod
    async def upsert(
        self,
        doc_id: str,
        chunks: list[dict[str, Any]],
        embeddings: list[list[float]],
        kb_id: str | None = None,
    ) -> int:
        """批量写入向量 + 元数据。"""
        ...

    @abstractmethod
    async def search(
        self,
        query_vec: list[float],
        top_k: int = 20,
        kb_ids: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """k-NN 向量检索。"""
        ...

    @abstractmethod
    async def fetch_by_ids(self, chunk_ids: list[str]) -> list[dict[str, Any]]:
        """按 ID 批量获取 chunk（父子索引回溯用）。"""
        ...
```

#### 3.4 OpenSearch 向量存储实现

OpenSearch 同时承担全文检索（BM25）和向量检索（k-NN）两个角色，只需一个引擎：

```python
import httpx

from app.config import get_settings
from app.rag.vector_store.base import VectorStoreBase


class OpenSearchVectorStore(VectorStoreBase):
    """OpenSearch k-NN 向量存储 — HNSW + cosine 相似度。"""

    def __init__(self, dimension: int = 3072) -> None:
        settings = get_settings()
        self._base_url = settings.OPENSEARCH_URL
        self._index_name = "ekb_chunks"
        self._dimension = dimension
        self._http = httpx.AsyncClient(timeout=30.0)

    async def ensure_index(self) -> None:
        """确保索引存在，不存在则创建。"""
        # 检查索引是否存在
        resp = await self._http.head(f"{self._base_url}/{self._index_name}")
        if resp.status_code == 200:
            return

        # 创建索引（含 k-NN 映射）
        mapping = {
            "settings": {
                "index": {"knn": True},
                "analysis": {
                    "analyzer": {
                        "ik_max_word": {"type": "custom", "tokenizer": "ik_max_word"}
                    }
                },
            },
            "mappings": {
                "properties": {
                    "content": {"type": "text", "analyzer": "ik_max_word"},
                    "title_path": {"type": "text", "boost": 2.0},
                    "embedding": {
                        "type": "knn_vector",
                        "dimension": self._dimension,
                        "method": {
                            "name": "hnsw",
                            "engine": "nmslib",
                            "space_type": "cosinesimil",
                        },
                    },
                    "doc_id": {"type": "keyword"},
                    "parent_id": {"type": "keyword"},
                    "kb_id": {"type": "keyword"},
                }
            },
        }
        await self._http.put(
            f"{self._base_url}/{self._index_name}", json=mapping
        )

    async def upsert(self, doc_id, chunks, embeddings, kb_id=None):
        """批量写入向量 + 元数据。"""
        await self.ensure_index()

        # 批量写入（Bulk API）
        bulk_lines = []
        for chunk, vec in zip(chunks, embeddings):
            action = {"index": {"_index": self._index_name, "_id": chunk["id"]}}
            doc = {
                "content": chunk["content"],
                "embedding": vec,
                "doc_id": doc_id,
                "parent_id": chunk.get("parent_id"),
                "kb_id": kb_id,
                "title_path": chunk.get("title_path", ""),
            }
            bulk_lines.append(action)
            bulk_lines.append(doc)

        bulk_body = "\n".join(
            [self._json(line) for line in bulk_lines]
        ) + "\n"

        resp = await self._http.post(
            f"{self._base_url}/_bulk",
            content=bulk_body,
            headers={"Content-Type": "application/x-ndjson"},
        )
        return len(chunks)

    async def search(self, query_vec, top_k=20, kb_ids=None):
        """k-NN 向量检索。"""
        query = {
            "size": top_k,
            "query": {
                "bool": {
                    "must": [
                        {
                            "knn": {
                                "embedding": {
                                    "vector": query_vec,
                                    "k": top_k,
                                }
                            }
                        }
                    ],
                    "filter": [],
                }
            },
        }
        if kb_ids:
            query["query"]["bool"]["filter"].append({"terms": {"kb_id": kb_ids}})

        resp = await self._http.post(
            f"{self._base_url}/{self._index_name}/_search", json=query
        )
        data = resp.json()
        results = []
        for hit in data.get("hits", {}).get("hits", []):
            source = hit["_source"]
            results.append(
                {
                    "chunk_id": hit["_id"],
                    "content": source.get("content", ""),
                    "doc_id": source.get("doc_id", ""),
                    "parent_id": source.get("parent_id"),
                    "title_path": source.get("title_path", ""),
                    "score": hit.get("_score", 0.0),
                }
            )
        return results

    async def fetch_by_ids(self, chunk_ids):
        """按 ID 批量获取 chunk（父子索引回溯用）。"""
        if not chunk_ids:
            return []
        query = {"query": {"ids": {"values": chunk_ids}}}
        resp = await self._http.post(
            f"{self._base_url}/{self._index_name}/_search", json=query
        )
        data = resp.json()
        results = []
        for hit in data.get("hits", {}).get("hits", []):
            source = hit["_source"]
            results.append(
                {
                    "chunk_id": hit["_id"],
                    "content": source.get("content", ""),
                    "doc_id": source.get("doc_id", ""),
                    "parent_id": source.get("parent_id"),
                    "title_path": source.get("title_path", ""),
                }
            )
        return results
```

`ik_max_word` 是中文分词器，需安装 OpenSearch IK 插件。英文环境可换 `standard`。

#### 3.5 向量存储工厂

```python
from functools import lru_cache

from app.rag.vector_store.base import VectorStoreBase
from app.rag.vector_store.opensearch_store import OpenSearchVectorStore


@lru_cache(maxsize=1)
def get_vector_store() -> VectorStoreBase:
    """获取向量存储单例。"""
    # LiteRAG 只有 OpenSearch，完整版可切换 Milvus
    return OpenSearchVectorStore()
```

---

### 4. 检索层

**目标**：多路召回候选文档，合并去重后交给重排器。

**核心文件**：`app/rag/retriever.py`

#### 4.1 混合检索（向量 + 全文）

```python
import httpx

from app.llm.embedder import get_embedder
from app.rag.vector_store import get_vector_store


class HybridRetriever:
    """混合检索器 — 向量 + 全文两路召回后合并去重。

    使用方式::

        retriever = HybridRetriever()
        candidates = await retriever.search("报销流程", kb_ids=[...], top_k=20)
    """

    def __init__(self) -> None:
        self._embedder = None  # 懒初始化
        self._vector_store = None  # 懒初始化
        self._http = httpx.AsyncClient(timeout=5.0)

    async def search(
        self,
        query: str,
        kb_ids: list[str] | None = None,
        top_k: int = 20,
    ) -> list[dict]:
        """混合检索 — 向量 + 全文并发召回。"""
        # 并发执行两路检索
        vector_results = await self._vector_search(query, kb_ids, top_k)
        fulltext_results = await self._fulltext_search(query, kb_ids, top_k)

        # 合并 + 按 chunk_id 去重
        merged = self._merge_and_dedupe(vector_results, fulltext_results)

        # 父块回溯
        expanded = await self._expand_to_parents(merged)
        return expanded
```

**向量检索**：用查询文本生成 embedding，在 OpenSearch k-NN 空间找最相似的 chunk。擅长语义匹配——用户问"费用报销"，能找到内容是"开支申请"的文档。

**全文检索**：用 BM25 算法在 OpenSearch 全文索引中检索。擅长精确匹配——用户搜"RFC-1234"这种编号，向量检索找不到，BM25 能精确命中。

两路并发执行（`asyncio.gather`），合并后按 `chunk_id` 去重。

#### 4.2 向量检索实现

```python
async def _vector_search(
    self, query: str, kb_ids: list[str] | None, top_k: int
) -> list[dict]:
    """向量检索 — query embedding 后 k-NN 查询。"""
    try:
        embedder = await self._get_embedder()
        if embedder is None:
            return []
        query_vec = (await embedder.embed([query]))[0]
        store = self._get_vector_store()
        return await store.search(query_vec, top_k=top_k, kb_ids=kb_ids)
    except Exception as e:
        log.warning("retriever.vector_search.failed", error=str(e))
        return []
```

#### 4.3 全文检索实现

```python
async def _fulltext_search(
    self, query: str, kb_ids: list[str] | None, top_k: int
) -> list[dict]:
    """全文检索 — OpenSearch BM25。"""
    try:
        settings = get_settings()
        query_body = {
            "size": top_k,
            "query": {
                "bool": {
                    "must": [{"match": {"content": query}}],
                    "filter": [],
                }
            },
        }
        if kb_ids:
            query_body["query"]["bool"]["filter"].append(
                {"terms": {"kb_id": kb_ids}}
            )

        resp = await self._http.post(
            f"{settings.OPENSEARCH_URL}/{settings.OPENSEARCH_INDEX}/_search",
            json=query_body,
        )
        resp.raise_for_status()
        data = resp.json()
        results = []
        for hit in data.get("hits", {}).get("hits", []):
            source = hit["_source"]
            results.append(
                {
                    "chunk_id": hit["_id"],
                    "content": source.get("content", ""),
                    "doc_id": source.get("doc_id", ""),
                    "parent_id": source.get("parent_id"),
                    "title_path": source.get("title_path", ""),
                    "score": hit.get("_score", 0.0),
                }
            )
        return results
    except Exception as e:
        log.warning("retriever.fulltext_search.failed", error=str(e))
        return []
```

#### 4.4 合并去重

```python
def _merge_and_dedupe(
    self, vector_results: list[dict], fulltext_results: list[dict]
) -> list[dict]:
    """合并两路结果，按 chunk_id 去重，保留较高分数。"""
    seen: dict[str, dict] = {}
    for r in vector_results + fulltext_results:
        cid = r["chunk_id"]
        if cid not in seen or r["score"] > seen[cid]["score"]:
            seen[cid] = r
    # 按分数降序
    return sorted(seen.values(), key=lambda x: x["score"], reverse=True)
```

#### 4.5 父块回溯

```python
async def _expand_to_parents(self, results: list[dict]) -> list[dict]:
    """命中子块后回取父块，扩充上下文。"""
    parent_ids = {r["parent_id"] for r in results if r.get("parent_id")}
    if not parent_ids:
        return results

    # 批量获取父块
    store = self._get_vector_store()
    parents = await store.fetch_by_ids(list(parent_ids))
    parent_map = {p["chunk_id"]: p for p in parents}

    # 用父块内容替换子块，按 parent_id 去重
    expanded = []
    seen_parents = set()
    for r in results:
        pid = r.get("parent_id")
        if pid and pid in parent_map and pid not in seen_parents:
            expanded.append({**r, "content": parent_map[pid]["content"]})
            seen_parents.add(pid)
        else:
            expanded.append(r)
    return expanded
```

#### 4.6 优雅降级

每路检索独立 try / except，任一路失败返回空列表，不影响另一路。OpenSearch 不可用时，检索返回空列表，API 返回"暂未找到相关文档"而不是 500 错误。

---

### 5. 重排层

**目标**：对检索召回的候选文档按相关性重新排序，让最相关的排到前面。

**核心文件**：`app/rag/reranker.py`

#### 5.1 为什么需要重排

向量检索和 BM25 都是"召回"操作，目标是找得全，不保证找得准。比如查询"报销流程"，可能召回 20 个 chunk，其中只有 3 个真正讲流程步骤，其余只是在其他上下文提到了"报销"这个词。

重排器用更强的模型（通常是 cross-encoder）对 query 和每个 candidate 做精细匹配，输出精确的相关性分数，把最相关的排到 top_k 之内。

#### 5.2 重排器抽象

```python
from abc import ABC, abstractmethod
from typing import Any


class RerankerBase(ABC):
    """重排器统一接口。"""

    @abstractmethod
    async def rerank(
        self,
        query: str,
        documents: list[str | dict[str, Any]],
        top_k: int = 5,
    ) -> list[dict[str, Any]]:
        """对文档列表重排。

        Returns:
            重排结果列表，每项格式:
            {"index": int, "score": float, "content": str}
        """
        ...
```

#### 5.3 Cohere 重排器实现

```python
import httpx

from app.config import get_settings
from app.rag.reranker import RerankerBase


class CohereReranker(RerankerBase):
    """SaaS 模式重排器 — Cohere Rerank 3.5。"""

    def __init__(self) -> None:
        settings = get_settings()
        self._api_key = settings.COHERE_API_KEY
        self._model = "rerank-multilingual-v3.0"
        self._http = httpx.AsyncClient(timeout=10.0)

    async def rerank(self, query, documents, top_k=5):
        if not documents:
            return []

        texts = [self._extract_content(d) for d in documents]
        resp = await self._http.post(
            "https://api.cohere.ai/v1/rerank",
            json={
                "model": self._model,
                "query": query,
                "documents": texts,
                "top_n": min(top_k, len(texts)),
            },
            headers={"Authorization": f"Bearer {self._api_key}"},
        )
        resp.raise_for_status()
        data = resp.json()

        results = []
        for item in data["results"]:
            idx = item["index"]
            results.append(
                {
                    "index": idx,
                    "score": float(item["relevance_score"]),
                    "content": texts[idx] if idx < len(texts) else "",
                }
            )
        return results

    @staticmethod
    def _extract_content(doc) -> str:
        if isinstance(doc, str):
            return doc
        if isinstance(doc, dict):
            return str(doc.get("content") or "")
        return str(doc)
```

#### 5.4 降级：跳过重排

如果没有 Cohere API Key，可以跳过重排层，直接用向量检索的 score 排序。在配置中设置 `RERANKER_ENABLED=false` 即可：

```python
class NoopReranker(RerankerBase):
    """空重排器 — 直接返回原始顺序。"""

    async def rerank(self, query, documents, top_k=5):
        texts = [self._extract_content(d) for d in documents[:top_k]]
        return [
            {"index": i, "score": 0.0, "content": t}
            for i, t in enumerate(texts)
        ]
```

重排失败时也回退原始顺序（score=0），不抛异常。这样即使 Cohere API 挂了，用户依然能拿到检索结果，只是排序质量下降。

---

### 6. 生成层

**目标**：组装上下文 prompt，调用 LLM 流式生成答案，提取引用标注。

**核心文件**：`app/rag/generator.py`、`app/rag/citation.py`

#### 6.1 LLM Provider 抽象

```python
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from typing import Any


class LLMProvider(ABC):
    """LLM 对话统一接口。"""

    @abstractmethod
    async def chat(
        self,
        messages: list[dict[str, str]],
        stream: bool = True,
        max_tokens: int = 4096,
    ) -> AsyncIterator[str]:
        """流式对话 — 逐 token yield。"""
        ...
```

#### 6.2 上下文组装

```python
_MAX_TOKENS: int = 4096
# Context Cliff 阈值 — 超过此值后 LLM 对中间位置信息提取能力下降
_CONTEXT_CLIFF_THRESHOLD: int = 2500
_CONTEXT_CLIFF_FALLBACK_TOP_K: int = 3
_DOC_MAX_CHARS: int = 1500


class Generator:
    """答案生成器 — 组装上下文并流式生成。"""

    def __init__(self, llm: LLMProvider) -> None:
        self.llm = llm

    async def generate(
        self,
        query: str,
        retrieved_docs: list[dict],
    ) -> AsyncIterator[str]:
        """流式生成答案，逐 token yield。"""
        system_prompt = self._build_system_prompt(retrieved_docs)
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": query},
        ]
        async for chunk in self.llm.chat(messages, stream=True, max_tokens=_MAX_TOKENS):
            if isinstance(chunk, str) and chunk:
                yield chunk
```

`_build_system_prompt` 把检索到的文档拼接为带编号的引用块：

```python
def _build_system_prompt(self, retrieved_docs: list[dict]) -> str:
    """组装系统 prompt — 注入检索上下文与引用指引。"""
    # Context Cliff 监控 — 上下文过长时自动降级
    context_docs = self._check_context_cliff(retrieved_docs)

    parts = [
        "你是企业知识库助手。请基于以下检索到的上下文回答用户问题。",
        "如果上下文不足以回答，请明确说明并建议补充信息。",
        "禁止编造未在上下文中出现的事实。",
    ]

    if context_docs:
        parts.append(
            "在引用知识库内容时，请使用 [n] 标注引用来源"
            "（n 从 1 开始，对应下方「知识库来源」的编号）。"
        )

    # 知识库来源（带编号）
    if context_docs:
        parts.append("\n=== 知识库来源 ===")
        for idx, doc in enumerate(context_docs, start=1):
            title = doc.get("title") or "未命名文档"
            title_path = doc.get("title_path", "")
            content = self._truncate(str(doc.get("content") or ""))
            if title_path:
                parts.append(f"[{idx}] {title_path}\n{content}")
            else:
                parts.append(f"[{idx}] {title}\n{content}")

    return "\n".join(parts)
```

#### 6.3 Context Cliff 降级

当注入上下文总 token 超过 2500 时，LLM 对中间位置信息的提取能力会显著下降（"Lost in the Middle"问题）。Generator 会自动裁剪上下文，只保留 top-3 最相关的 chunk：

```python
def _check_context_cliff(self, retrieved_docs: list[dict]) -> list[dict]:
    """检测并降级过长的注入上下文。"""
    if not retrieved_docs:
        return retrieved_docs

    total_tokens = sum(
        estimate_tokens(str(doc.get("content") or "")) for doc in retrieved_docs
    )

    if total_tokens <= _CONTEXT_CLIFF_THRESHOLD:
        return retrieved_docs

    # 触发降级 — 只保留 Top-3
    truncated = retrieved_docs[:_CONTEXT_CLIFF_FALLBACK_TOP_K]
    return truncated

@staticmethod
def _truncate(text: str, max_chars: int = _DOC_MAX_CHARS) -> str:
    """截断文档内容，避免 prompt 过长。"""
    text = text.strip()
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "..."
```

#### 6.4 引用标注提取

```python
import re


class CitationExtractor:
    """从生成文本中提取 [n] 引用，关联到源文档。"""

    _PATTERN = re.compile(r"\[(\d+)\]")

    def extract(
        self, answer: str, sources: list[dict]
    ) -> list[dict]:
        """提取引用信息。

        Args:
            answer: LLM 生成的答案文本。
            sources: 检索到的文档列表。

        Returns:
            引用列表，每项包含 index / doc_id / title。
        """
        citations = []
        seen = set()
        for match in self._PATTERN.finditer(answer):
            idx = int(match.group(1)) - 1  # 转为 0-based
            if 0 <= idx < len(sources) and idx not in seen:
                seen.add(idx)
                citations.append(
                    {
                        "index": idx + 1,
                        "doc_id": sources[idx].get("doc_id", ""),
                        "title": sources[idx].get("title", ""),
                    }
                )
        return citations
```

---

### 7. API 层

**目标**：暴露 RESTful API 供前端或第三方调用。

**核心文件**：`app/api/v1/`

#### 7.1 核心 API 端点

| 方法 | 路径 | 功能 |
|------|------|------|
| POST | `/api/v1/knowledge` | 创建知识库 |
| GET | `/api/v1/knowledge` | 列出知识库 |
| POST | `/api/v1/documents/upload` | 上传文档 |
| GET | `/api/v1/documents/{doc_id}/progress` | 查询文档处理进度 |
| POST | `/api/v1/search` | 搜索知识库 |
| POST | `/api/v1/chat/stream` | 流式对话（SSE） |
| GET | `/api/v1/health` | 健康检查 |

#### 7.2 文档上传流程

```
POST /documents/upload
    │
    ▼
保存文件到本地
    │
    ▼
创建 Document 记录（status=pending）
    │
    ▼
异步处理（asyncio.create_task，不用 Celery）：
    1. parse_document(file_path) → text
    2. chunk_document(text) → chunks
    3. embed(chunks) → embeddings
    4. vector_store.upsert(chunks, embeddings)
    5. opensearch_index.create(chunks)  # 全文索引
    │
    ▼
更新 Document status=published
```

LiteRAG 砍掉了 Celery，文档处理直接在请求中异步执行（`asyncio.create_task`）。适合学习和小规模使用。完整版用 Celery + chord 编排并行管线，适合生产环境。

#### 7.3 文档上传接口实现

```python
from fastapi import APIRouter, UploadFile, File, Form, BackgroundTasks

router = APIRouter(prefix="/api/v1/documents", tags=["documents"])


@router.post("/upload")
async def upload_document(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    kb_id: str = Form(...),
):
    """上传文档到知识库。"""
    # 1. 保存文件
    file_path = await _save_upload(file)

    # 2. 创建 Document 记录
    doc = await document_repo.create(
        title=file.filename,
        file_path=file_path,
        doc_type=_get_doc_type(file.filename),
        kb_id=kb_id,
        status="pending",
    )

    # 3. 后台异步处理
    background_tasks.add_task(_process_document, doc.id, file_path, kb_id)

    return {"doc_id": doc.id, "status": "pending"}


async def _process_document(doc_id: str, file_path: str, kb_id: str):
    """文档处理管线 — 解析 → 分块 → 向量化 → 存储。"""
    try:
        # 更新进度
        await _update_progress(doc_id, "parsing", 0.1)

        # 1. 解析
        parser = get_parser(_get_doc_type(file_path))
        text = await parser.parse(file_path)

        await _update_progress(doc_id, "chunking", 0.3)

        # 2. 分块
        chunks = chunk_document(text, doc_id=doc_id)

        await _update_progress(doc_id, "embedding", 0.5)

        # 3. 向量化
        embedder = get_embedder()
        embeddings = await embedder.embed([c.content for c in chunks])

        await _update_progress(doc_id, "indexing", 0.8)

        # 4. 存储到向量库
        store = get_vector_store()
        chunk_dicts = [
            {
                "id": c.id,
                "content": c.content,
                "parent_id": c.parent_id,
                "title_path": c.title_path,
            }
            for c in chunks
        ]
        await store.upsert(doc_id, chunk_dicts, embeddings, kb_id=kb_id)

        # 5. 更新状态
        await document_repo.update_status(doc_id, "published")
        await _update_progress(doc_id, "done", 1.0)

    except Exception as e:
        await document_repo.update_status(doc_id, "failed")
        log.error("document.processing.failed", doc_id=doc_id, error=str(e))
```

#### 7.4 流式对话接口

```python
import json
from fastapi import APIRouter
from fastapi.responses import StreamingResponse

router = APIRouter(prefix="/api/v1/chat", tags=["chat"])


@router.post("/stream")
async def chat_stream(req: ChatRequest):
    """流式对话 — SSE 返回。"""

    async def event_stream():
        # 1. 检索
        retriever = HybridRetriever()
        results = await retriever.search(req.query, kb_ids=req.kb_ids)

        # 2. 重排
        reranker = get_reranker()
        reranked = await reranker.rerank(req.query, results, top_k=5)

        # 3. 生成
        generator = Generator(get_llm())
        async for token in generator.generate(req.query, reranked):
            yield f"data: {json.dumps({'token': token})}\n\n"

        # 4. 引用
        citations = generator.extract_citations(
            collected_answer, reranked
        )
        yield f"data: {json.dumps({'citations': citations, 'done': True})}\n\n"

    return StreamingResponse(
        event_stream(), media_type="text/event-stream"
    )
```

前端用 `EventSource` 或 `fetch + ReadableStream` 接收 SSE 流，逐 token 渲染答案。

#### 7.5 FastAPI 入口

```python
from fastapi import FastAPI
from contextlib import asynccontextmanager


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期 — 启动时初始化、关闭时清理。"""
    # 启动：确保 OpenSearch 索引存在
    store = get_vector_store()
    await store.ensure_index()
    yield
    # 关闭：清理资源
    await store.close()


app = FastAPI(
    title="LiteRAG",
    description="从零搭建的生产级 RAG 知识库",
    version="1.0.0",
    lifespan=lifespan,
)

# 挂载路由
from app.api.v1 import knowledge, documents, search, chat

app.include_router(knowledge.router)
app.include_router(documents.router)
app.include_router(search.router)
app.include_router(chat.router)


@app.get("/api/v1/health")
async def health():
    return {"status": "ok"}
```

---

### 8. Agent Loop 层

**目标**：让 RAG 从"单次检索 + 生成"升级为"思考 → 执行 → 反思"的迭代循环，这是智能体区别于普通 RAG 管线的核心。

**核心文件**：`app/agents/`

#### 8.1 为什么需要 Agent Loop

普通 RAG 是一条直线：检索 → 生成 → 结束。如果第一次检索没找到相关内容，或者生成的答案质量差，系统没有机会纠正自己。

Agent Loop 引入"反思"机制：生成答案后检查质量，不达标就重试，最多迭代 N 次。这让系统具备了自我纠错能力。

LiteRAG 用 **LangGraph StateGraph** 实现 Agent Loop——把 think / execute / reflect 三个阶段定义为图的节点，用条件边控制"通过则结束，不通过则重试"的循环。这比手写 for 循环更清晰：状态流转是显式的，每个节点的输入输出都有类型约束，而且天然支持流式输出。

```
普通 RAG:  检索 → 生成 → 结束（一次性，无法纠错）

LangGraph StateGraph:
  ┌─────────────────────────────────────────────┐
  │  START                                        │
  │    ↓                                          │
  │  [init] — 加载记忆 + 初始化状态               │
  │    ↓                                          │
  │  [think] ──────┐ (条件边)                     │
  │    ↓           │                              │
  │  [execute]     │ 检索 → 构建上下文 → 流式生成   │
  │    ↓           │                              │
  │  [reflect] ────┤                              │
  │    ├─ 通过 → [save_memory] → END              │
  │    └─ 不通过 ──┘ (回到 think，最多 5 轮)       │
  └─────────────────────────────────────────────┘
```

#### 8.2 Agent 抽象基类

`base.py` 用 LangGraph 的 `StateGraph` 定义 Agent Loop 主循环。think / execute / reflect 是图的三个节点，reflect 的返回值决定走哪条条件边（重试 or 结束）。子类只需实现 `execute()` 方法：

```python
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from typing import Any, TypedDict, Annotated

from langgraph.graph import StateGraph, START, END
from langgraph.graph.message import add_messages


class AgentState(TypedDict, total=False):
    """Agent 运行时状态 — LangGraph StateGraph 的共享状态。

    每个节点接收完整 state，返回的 dict 会被 merge 回 state。
    """
    query: str               # 用户原始查询
    messages: Annotated[list[dict], add_messages]  # 消息列表（LangGraph 自动累加）
    retrieved_docs: list[dict]  # 检索到的文档
    answer: str              # 当前生成的答案
    iteration: int           # 当前迭代轮次
    user_id: str             # 用户 ID（记忆引擎用）
    session_id: str           # 会话 ID


class BaseAgent(ABC):
    """Agent 抽象基类 — think→execute→reflect 命令式主循环
    （LangGraph StateGraph 为引擎层可选声明式路径，见 rag/engine.py）。"""

    agent_type: str = "base"
    system_prompt: str = "你是一个企业知识库 AI 助手。"
    max_iterations: int = 5  # 防止无限循环

    def __init__(self, llm: LLMProvider, memory: MemoryManager) -> None:
        self.llm = llm
        self.memory = memory
        self._compiled = None  # 延迟编译

    def _build_graph(self) -> Any:
        """构建 LangGraph StateGraph — think → execute → reflect 循环。"""
        graph = StateGraph(AgentState)

        # 注册节点
        graph.add_node("init", self._init_node)
        graph.add_node("execute", self._execute_wrapper)
        graph.add_node("reflect", self._reflect_node)
        graph.add_node("save_memory", self._save_memory_node)

        # 连边：START → init → execute → reflect
        graph.add_edge(START, "init")
        graph.add_edge("init", "execute")
        graph.add_edge("execute", "reflect")

        # 条件边：reflect 通过 → save_memory → END；不通过 → 回到 execute
        graph.add_conditional_edges(
            "reflect",
            self._should_retry,
            {
                "retry": "execute",       # 反思未通过，重试
                "done": "save_memory",     # 反思通过，保存记忆
            },
        )
        graph.add_edge("save_memory", END)

        return graph.compile()

    # ------------------------------------------------------------------
    # 节点实现
    # ------------------------------------------------------------------

    async def _init_node(self, state: AgentState) -> dict:
        """初始化节点 — 加载记忆上下文，注入到 messages。"""
        query = state["query"]
        user_id = state.get("user_id", "")
        session_id = state.get("session_id", "")

        # 加载记忆（失败时返回空上下文，不影响主流程）
        memory_ctx = await self._load_memory(user_id, session_id, query)

        return {
            "messages": [
                {"role": "system", "content": self.system_prompt + memory_ctx},
                {"role": "user", "content": query},
            ],
            "iteration": 0,
            "answer": "",
        }

    async def _execute_wrapper(self, state: AgentState) -> dict:
        """执行节点包装器 — 调用子类的 execute()，收集答案。

        注意：LangGraph 节点返回 dict 更新 state。
        流式 token 通过 async generator 单独处理（见 run()）。
        """
        # 递增迭代计数
        iteration = state.get("iteration", 0) + 1

        # 调用子类 execute — 收集完整答案
        answer_parts = []
        async for token in self.execute(state):
            answer_parts.append(token)

        return {
            "answer": "".join(answer_parts),
            "iteration": iteration,
        }

    async def _reflect_node(self, state: AgentState) -> dict:
        """反思节点 — 返回空 dict（不修改 state）。

        实际的判断逻辑在 _should_retry() 中通过条件边实现。
        """
        return {}

    async def _save_memory_node(self, state: AgentState) -> dict:
        """保存记忆节点 — 失败时仅记日志，不影响主流程。"""
        try:
            await self.memory.save_session(
                state.get("user_id", ""),
                state.get("session_id", ""),
                state,
                summary=state.get("answer"),
            )
        except Exception as e:
            log.warning("agent.save_memory.failed", error=str(e))
        return {}

    def _should_retry(self, state: AgentState) -> str:
        """条件边判断 — 返回 'retry' 或 'done'。

        简化版规则：答案不足 10 字符且未超过最大迭代次数则重试。
        """
        answer = state.get("answer", "")
        iteration = state.get("iteration", 0)
        if len(answer) < 10 and iteration < self.max_iterations:
            return "retry"
        return "done"

    # ------------------------------------------------------------------
    # 子类实现
    # ------------------------------------------------------------------

    @abstractmethod
    async def execute(self, state: AgentState) -> AsyncIterator[str]:
        """执行阶段 — 子类实现核心逻辑，流式 yield token。"""
        ...

    # ------------------------------------------------------------------
    # 运行入口
    # ------------------------------------------------------------------

    async def run(
        self, query: str, user_id: str, session_id: str
    ) -> AsyncIterator[str]:
        """运行 Agent Loop — 流式输出答案。

        LangGraph 的 astream_events 支持节点内部 async generator 的
        token 级流式输出，这里用 events 模式捕获 execute 节点的 yield。
        """
        if self._compiled is None:
            self._compiled = self._build_graph()

        # 先执行 execute 节点的流式部分，再驱动图完成
        # 简化实现：先流式输出 execute 的 token，再让 graph 跑完 reflect + save
        state: AgentState = {
            "query": query,
            "messages": [],
            "answer": "",
            "iteration": 0,
            "user_id": user_id,
            "session_id": session_id,
        }

        # 手动驱动节点流转（简化版，保留 LangGraph 的状态图结构但用流式方式跑）
        for i in range(self.max_iterations):
            state["iteration"] = i

            # init 节点（仅首轮）
            if i == 0:
                init_result = await self._init_node(state)
                state.update(init_result)

            # execute — 流式输出
            answer_parts = []
            async for token in self.execute(state):
                answer_parts.append(token)
                yield token
            state["answer"] = "".join(answer_parts)

            # reflect — 判断是否重试
            if self._should_retry(state) == "done":
                break

        # save_memory
        await self._save_memory_node(state)

    async def _load_memory(self, user_id, session_id, query) -> str:
        """加载记忆上下文 — 失败时返回空字符串。"""
        try:
            ctx = await self.memory.build_context(user_id, session_id)
            return ctx.to_system_prompt()
        except Exception:
            return ""
```

#### 8.3 QA Agent — Agentic RAG

`QAAgent` 继承 `BaseAgent`，实现 Agentic RAG 问答。`execute()` 方法完成"检索 → 构建上下文 → 流式生成"三步：

```python
class QAAgent(BaseAgent):
    """QA Agent — Agentic RAG 问答。"""

    agent_type = "qa"
    system_prompt = (
        "你是一个企业知识库问答助手。"
        "请基于检索到的上下文回答用户问题。"
        "如果上下文不足以回答，请明确说明。"
        "禁止编造未在上下文中出现的事实。"
    )

    async def execute(self, state: AgentState) -> AsyncIterator[str]:
        """检索 → 构建上下文 → 流式生成。"""
        query = state["query"]

        # 1. 检索
        state["retrieved_docs"] = await self._retrieve(query)

        # 2. 构建上下文
        context = self._build_context(state["retrieved_docs"])
        if context:
            state["messages"].append({
                "role": "system",
                "content": f"知识库来源：\n{context}",
            })

        # 3. 流式生成
        answer_parts = []
        async for chunk in self.llm.chat(state["messages"], stream=True):
            if isinstance(chunk, str) and chunk:
                answer_parts.append(chunk)
                yield chunk

        state["answer"] = "".join(answer_parts)

    async def _retrieve(self, query: str) -> list[dict]:
        """调用检索器获取相关文档。"""
        try:
            retriever = HybridRetriever()
            return await retriever.search(query, top_k=20)
        except Exception:
            return []

    @staticmethod
    def _build_context(docs: list[dict]) -> str:
        """把检索结果格式化为带编号的引用块。"""
        if not docs:
            return ""
        parts = []
        for idx, doc in enumerate(docs[:5], start=1):
            title = doc.get("title", "未命名文档")
            content = doc.get("content", "")[:500]
            parts.append(f"[{idx}] {title}\n{content}")
        return "\n\n".join(parts)
```

#### 8.4 Agent 注册表

`registry.py` 用注册表模式实现"新增 Agent 类型不改工厂逻辑"：

```python
class AgentRegistry:
    """Agent 注册表 — agent_type → Agent 类映射。"""

    _registry: dict[str, type[BaseAgent]] = {}

    @classmethod
    def register(cls, agent_type: str):
        """装饰器：注册 Agent 类型。"""
        def decorator(agent_cls: type[BaseAgent]):
            cls._registry[agent_type] = agent_cls
            return agent_cls
        return decorator

    @classmethod
    def create(
        cls, agent_type: str, llm: LLMProvider, memory: MemoryManager
    ) -> BaseAgent:
        """工厂方法：按类型创建 Agent 实例。"""
        agent_cls = cls._registry.get(agent_type)
        if agent_cls is None:
            raise ValueError(f"未注册的 Agent 类型: {agent_type}")
        return agent_cls(llm, memory)

    @classmethod
    def list_agents(cls) -> dict[str, str]:
        """列出已注册的 Agent（调试用）。"""
        return {k: v.__name__ for k, v in cls._registry.items()}


# 自动注册内置 Agent
AgentRegistry.register("qa")(QAAgent)
```

使用方式：

```python
# 创建 QA Agent
agent = AgentRegistry.create("qa", llm_provider, memory_manager)

# 运行 Agent Loop
async for token in agent.run("报销流程是什么？", user_id, session_id):
    print(token, end="", flush=True)  # 流式输出

# 注册自定义 Agent
@AgentRegistry.register("custom")
class CustomAgent(BaseAgent):
    async def execute(self, state):
        # 你的自定义逻辑
        ...
```

#### 8.5 砍掉了什么

| 完整版能力 | LiteRAG 处理 | 理由 |
|-----------|-------------|------|
| think() 调 LLM 决策 | 简化为 no-op | 每轮省 100 token，内置 Agent 不需要决策分支 |
| ActionAgent（工单创建） | 砍掉 | 执行型操作超出 RAG 范畴 |
| WorkflowAgent（审批流程） | 砍掉 | 业务流程引导需要 OA 系统对接 |
| CrewAI 多 Agent 协作 | 砍掉 | 多 Agent 编排适合进阶课程 |
| MCP 工具协议 | 砍掉 | 工具调用协议增加理解成本 |
| LangGraph Checkpointer 持久化 | 砍掉 | 简化版不需要中断恢复，进阶时可启用 PostgreSQL Checkpointer |
| LangGraph Human-in-the-Loop | 砍掉 | 敏感操作审批适合企业级场景，学习版不需要 |
| LangGraph 时间旅行调试 | 砍掉 | 回溯历史状态适合生产调试，学习版用 print 足够 |

#### 8.6 你需要实现的文件

```
app/agents/
├── __init__.py       # 导出 + 触发自动注册
├── base.py           # BaseAgent + AgentState
├── qa_agent.py       # QAAgent
└── registry.py       # AgentRegistry
```

---

### 9. 记忆引擎层

**目标**：让 Agent 具备"记忆"能力——记住用户偏好、对话历史、关键事实，在后续对话中注入上下文，而不是每次都从零开始。

**核心文件**：`app/memory/`

#### 9.1 为什么需要记忆引擎

没有记忆的 RAG 每次对话都是孤立的。用户说"我喜欢简洁的回答"，下一轮对话系统就忘了。用户问"刚才说的那个报销流程"，系统不知道"刚才"指什么。

记忆引擎解决两个问题：
1. **跨轮记忆** — 记住当前对话的上下文（"刚才说的"指什么）
2. **跨会话记忆** — 记住用户偏好（"我喜欢简洁回答"）

#### 9.2 两级记忆架构（简化版）

完整版有四级记忆（L1 短期 / L2 检查点 / L3 Mem0 偏好 / L4 工作记忆）。LiteRAG 简化为两级：

```
┌─────────────────────────────────────────────────┐
│  L1: 短期窗口                                    │
│  当前对话最近 20 条消息                          │
│  承载方式：内存中的 list                          │
│  作用：跨轮上下文（"刚才说的"指什么）              │
├─────────────────────────────────────────────────┤
│  L2: 用户偏好（简化版 Mem0）                      │
│  跨会话的用户偏好和历史摘要                       │
│  承载方式：PostgreSQL user_facts 表               │
│  作用：跨会话记忆（"我喜欢简洁回答"）              │
└─────────────────────────────────────────────────┘
```

| 层级 | 完整版 | LiteRAG | 差异 |
|------|--------|---------|------|
| L1 短期窗口 | 最近 20 条消息 | 最近 20 条消息 | 相同 |
| L2 检查点 | LangGraph 状态快照（PostgreSQL） | 砍掉 | 简化版不需要中断恢复 |
| L3 用户偏好 | Mem0 + Embedding 语义检索 | PostgreSQL 关键词匹配 | 砍掉向量检索，用简单 SQL |
| L4 工作记忆 | 当前任务实体关系 | 砍掉 | 简化版不需要工作记忆 |

#### 9.3 记忆上下文

`MemoryContext` 聚合所有记忆层，渲染为 system prompt 片段注入 LLM：

```python
from dataclasses import dataclass, field


@dataclass
class MemoryContext:
    """记忆上下文 — 聚合 L1 短期窗口和 L2 用户偏好。"""

    short_term: list[dict] = field(default_factory=list)  # L1
    user_facts: list[dict] = field(default_factory=list)   # L2

    def to_system_prompt(self) -> str:
        """渲染为 system prompt 片段。"""
        parts = []

        # L1: 最近对话上下文
        if self.short_term:
            parts.append("=== 最近对话 ===")
            for msg in self.short_term[-8:]:  # 注入最近 8 条
                role = msg.get("role", "user")
                content = str(msg.get("content", ""))[:200]
                parts.append(f"{role}: {content}")

        # L2: 用户偏好
        preferences = [f for f in self.user_facts if f.get("category") == "preference"]
        if preferences:
            parts.append("\n=== 用户偏好 ===")
            for fact in preferences[:3]:
                parts.append(f"- {fact['fact_text']}")

        return "\n".join(parts) if parts else ""
```

#### 9.4 记忆管理器

`MemoryManager` 是编排器，协调两级记忆的读写：

```python
from app.memory.memory_context import MemoryContext

# L1 短期窗口大小
SHORT_TERM_WINDOW_SIZE = 20


class MemoryManager:
    """记忆引擎编排器 — 协调 L1 短期窗口和 L2 用户偏好。"""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def build_context(
        self,
        user_id: str,
        session_id: str | None = None,
        recent_messages: list[dict] | None = None,
    ) -> MemoryContext:
        """构建记忆上下文 — 每层独立容错。"""
        ctx = MemoryContext()

        # L1: 短期窗口
        if recent_messages:
            ctx.short_term = recent_messages[-SHORT_TERM_WINDOW_SIZE:]

        # L2: 用户偏好
        try:
            ctx.user_facts = await self._load_facts(user_id)
        except Exception as e:
            log.warning("memory.load_facts.failed", error=str(e))

        return ctx

    async def save_session(
        self,
        user_id: str,
        session_id: str,
        state: dict,
        summary: str | None = None,
    ) -> None:
        """对话结束后保存记忆。"""
        # 保存对话摘要（7 天过期）
        if summary:
            await self._save_fact(
                user_id, summary, category="summary", ttl_hours=168
            )

    async def extract_and_save_facts(
        self,
        user_id: str,
        messages: list[dict],
    ) -> list[str]:
        """从对话中提取用户偏好并保存。

        简化版用关键词启发式，完整版用 LLM 提取。
        """
        facts = []
        keywords = ["我喜欢", "我偏好", "请用", "请使用", "我希望"]

        for msg in messages:
            content = msg.get("content", "")
            for kw in keywords:
                if kw in content:
                    # 提取关键词后的内容
                    fact = content[content.index(kw):].strip()[:200]
                    await self._save_fact(user_id, fact, category="preference")
                    facts.append(fact)
                    break

        return facts

    async def _load_facts(self, user_id: str) -> list[dict]:
        """从 PostgreSQL 加载用户偏好。"""
        result = await self.db.execute(
            text("""
                SELECT fact_text, category FROM user_facts
                WHERE user_id = :uid AND is_active = true
                  AND (expires_at IS NULL OR expires_at > NOW())
                ORDER BY created_at DESC LIMIT 10
            """),
            {"uid": user_id},
        )
        return [dict(row) for row in result]

    async def _save_fact(
        self, user_id: str, fact_text: str,
        category: str = "working", ttl_hours: int | None = None,
    ) -> None:
        """保存一条用户事实。"""
        from datetime import datetime, timedelta

        expires_at = None
        if ttl_hours:
            expires_at = datetime.utcnow() + timedelta(hours=ttl_hours)

        await self.db.execute(
            text("""
                INSERT INTO user_facts (user_id, fact_text, category, expires_at)
                VALUES (:uid, :text, :cat, :exp)
            """),
            {"uid": user_id, "text": fact_text, "cat": category, "exp": expires_at},
        )
        await self.db.flush()
```

#### 9.5 记忆与 Agent 的集成

Agent 在 Loop 开始时加载记忆，结束时保存记忆：

```python
# BaseAgent.run() 中的集成（简化示意）

# Loop 开始 — 加载记忆
memory_ctx = await self.memory.build_context(user_id, session_id, recent_messages)
system_prompt += memory_ctx.to_system_prompt()

# ... 执行 Agent Loop ...

# Loop 结束 — 保存记忆
await self.memory.save_session(user_id, session_id, state, summary=state.get("answer"))
await self.memory.extract_and_save_facts(user_id, state["messages"])
```

#### 9.6 砍掉了什么

| 完整版能力 | LiteRAG 处理 | 理由 |
|-----------|-------------|------|
| L2 检查点（LangGraph 状态快照） | 砍掉 | 中断恢复场景少见，简化版不需要 |
| L4 工作记忆（当前任务实体） | 砍掉 | 需要实体抽取，增加复杂度 |
| Embedding 语义检索事实 | 改为关键词 SQL | 简化版不需要向量检索 |
| Graphiti 时序图谱 | 砍掉 | 知识演化追踪适合进阶 |
| LLM 提取事实 | 改为关键词启发式 | 省 token，规则覆盖常见场景 |
| TTL 过期机制 | 保留（简化版） | 对话摘要 7 天过期，偏好永不过期 |

#### 9.7 你需要实现的文件

```
app/memory/
├── __init__.py           # 导出 MemoryManager + MemoryContext
├── memory_context.py     # MemoryContext 数据类
└── memory_manager.py     # MemoryManager 编排器
```

---

### 10. 上下文工程层

**目标**：让对话更"聪明"——追踪当前话题焦点，消解省略句中的指代，让检索能理解用户的真实意图。

**核心文件**：`app/context/`

#### 10.1 为什么需要上下文工程

没有上下文工程的 RAG 把每轮对话当作独立查询。用户的多轮对话会变成这样：

```
用户: 北京今天车辆限号多少？
系统: 北京今天限号尾号 3 和 8。

用户: 那上海呢？          ← 系统不知道"那"指什么
系统: 抱歉，我不理解你的问题。

用户: 他什么时候开始的？   ← 系统不知道"他"指什么
系统: 抱歉，请提供更多信息。
```

有了上下文工程，系统能理解"那上海呢"的完整含义是"上海今天车辆限号多少"，"他什么时候开始的"的"他"指代当前对话焦点实体。

#### 10.2 焦点追踪

`focus_tracker.py` 从对话历史中提取当前焦点（主题、实体、意图），为后续的指代消解和检索提供上下文：

```python
from dataclasses import dataclass


@dataclass
class ConversationFocus:
    """对话焦点 — 当前话题、实体、意图。"""

    topic: str           # 当前话题，如 "限号政策"
    entity: str          # 主体实体，如 "北京"
    intent: str = "查询"  # 查询 / 操作 / 对比
    confidence: float = 0.5

    def to_context_str(self) -> str:
        """渲染为检索 prompt 片段。"""
        return f"话题: {self.topic} | 实体: {self.entity} | 意图: {self.intent}"


class TopicTracker:
    """焦点追踪器 — 规则优先，LLM 兜底。

    简化版只用关键词规则，完整版增加 LLM 提取。
    """

    # 内置话题关键词表
    _TOPIC_KEYWORDS = {
        "天气": ["天气", "气温", "下雨", "温度"],
        "限号": ["限号", "限行", "尾号"],
        "报销": ["报销", "费用", "发票", "报销单"],
        "请假": ["请假", "调休", "年假"],
        "合同": ["合同", "协议", "条款"],
        "采购": ["采购", "供应商", "报价"],
    }

    def extract_focus(self, history: list[dict]) -> ConversationFocus | None:
        """从对话历史提取当前焦点 — 纯规则，零 token 消耗。"""
        if not history:
            return None

        # 取最近 3 轮用户消息
        recent = [
            m["content"] for m in history[-6:]
            if m.get("role") == "user"
        ]
        if not recent:
            return None

        # 合并文本用于关键词匹配
        combined = " ".join(recent)

        # 匹配话题
        topic = ""
        for t, keywords in self._TOPIC_KEYWORDS.items():
            if any(kw in combined for kw in keywords):
                topic = t
                break

        if not topic:
            return None

        # 提取实体（简化版：找城市名）
        cities = ["北京", "上海", "广州", "深圳", "杭州", "成都"]
        entity = next((c for c in cities if c in combined), "")

        # 推断意图
        intent = "查询"
        if any(w in combined for w in ["对比", "比较", "区别"]):
            intent = "对比"
        elif any(w in combined for w in ["创建", "提交", "申请", "办理"]):
            intent = "操作"

        return ConversationFocus(
            topic=topic, entity=entity, intent=intent, confidence=0.8
        )
```

#### 10.3 指代消解

`coreference_resolver.py` 将省略句补全为完整查询，让检索能理解用户的真实意图：

```python
class CoreferenceResolver:
    """指代消解器 — 规则优先，LLM 兜底。

    检测省略句（"那上海呢？"）并补全为完整查询
    （"上海今天车辆限号多少？"）。
    """

    # 省略特征词 — 出现这些词可能需要消解
    _ELLIPSIS_INDICATORS = {"呢", "怎么样", "他", "她", "它", "这个", "那个", "刚才"}

    # 明确动词 — 出现这些词说明是完整查询，不需要消解
    _EXPLICIT_VERBS = {"搜索", "查找", "创建", "什么是", "查询", "如何"}

    def needs_resolution(self, query: str) -> bool:
        """判断是否需要指代消解。

        启发式：长度 < 30 且含省略特征词 且 不含明确动词。
        """
        if len(query) >= 30:
            return False
        if any(v in query for v in self._EXPLICIT_VERBS):
            return False
        return any(w in query for w in self._ELLIPSIS_INDICATORS)

    def resolve(
        self,
        query: str,
        focus: ConversationFocus | None,
    ) -> str:
        """消解指代，返回补全后的查询。

        无焦点或不需要消解时原样返回。
        """
        if not focus or not self.needs_resolution(query):
            return query

        # 规则消解：移除省略词 + 拼接焦点话题
        result = query
        for w in self._ELLIPSIS_INDICATORS:
            result = result.replace(w, "").strip()

        # 补全为完整查询
        if focus.entity and focus.entity not in result:
            result = f"{focus.entity}的{focus.topic} {result}".strip()
        elif focus.topic and focus.topic not in result:
            result = f"{focus.topic} {result}".strip()

        return result if result else query
```

#### 10.4 上下文工程与 Agent 的集成

在 Agent Loop 的检索阶段前，先做焦点追踪和指代消解：

```python
# QAAgent.execute() 中的集成（简化示意）

async def execute(self, state: AgentState) -> AsyncIterator[str]:
    query = state["query"]
    history = state.get("messages", [])

    # 1. 焦点追踪
    focus = self.topic_tracker.extract_focus(history)

    # 2. 指代消解 — 补全省略句
    resolved_query = self.coreference_resolver.resolve(query, focus)

    # 3. 用补全后的查询检索
    state["retrieved_docs"] = await self._retrieve(resolved_query)

    # 4. 构建上下文 + 流式生成（同基础版）
    ...
```

效果对比：

```
用户: 北京今天车辆限号多少？
系统: 北京今天限号尾号 3 和 8。

用户: 那上海呢？
  ↓ 焦点追踪: topic=限号, entity=北京
  ↓ 指代消解: "那上海呢？" → "上海的限号"
  ↓ 实际检索: "上海的限号"
系统: 上海今天不限号。
```

#### 10.5 砍掉了什么

| 完整版能力 | LiteRAG 处理 | 理由 |
|-----------|-------------|------|
| LLM 提取焦点 | 改为关键词规则 | 省 token，规则覆盖常见场景 |
| LLM 消解指代 | 改为规则拼接 | 省 token，简单场景够用 |
| 漂移检测（话题切换） | 砍掉 | 需要 Embedding 相似度计算 |
| 矛盾检测（答案冲突） | 砍掉 | 需要 LLM 判断，增加成本 |
| 上下文选择（向量筛选历史） | 砍掉 | 需要 Embedding，简化版用固定窗口 |
| 对话摘要（滚动压缩） | 砍掉 | 需要 LLM 压缩，简化版直接截断 |
| 焦点栈（多轮回溯） | 砍掉 | 增加状态管理复杂度 |

#### 10.6 你需要实现的文件

```
app/context/
├── __init__.py                # 导出 TopicTracker + CoreferenceResolver
├── focus_tracker.py           # ConversationFocus + TopicTracker
└── coreference_resolver.py     # CoreferenceResolver
```

---

## 数据库设计

### 核心表结构

LiteRAG 只需要 4 张表（完整版有 20+ 张）：

```sql
-- 知识库表
CREATE TABLE knowledge_base (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name VARCHAR(200) NOT NULL,
    description TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- 文档表
CREATE TABLE document (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    kb_id UUID REFERENCES knowledge_base(id) ON DELETE CASCADE,
    title VARCHAR(500) NOT NULL,
    file_path VARCHAR(1000),
    doc_type VARCHAR(20) DEFAULT 'md',  -- pdf / docx / md
    content_text TEXT,                  -- 解析后的纯文本
    content_hash VARCHAR(64),           -- 内容哈希（去重用）
    status VARCHAR(20) DEFAULT 'pending', -- pending / processing / published / failed
    chunk_count INTEGER DEFAULT 0,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- 处理进度表（可选，也可用 Redis）
CREATE TABLE parse_progress (
    doc_id UUID PRIMARY KEY REFERENCES document(id) ON DELETE CASCADE,
    stage VARCHAR(50),      -- parsing / chunking / embedding / indexing / done
    progress FLOAT DEFAULT 0,
    message VARCHAR(500),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- 用户事实表（记忆引擎用 — 存储用户偏好和对话摘要）
CREATE TABLE user_facts (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id VARCHAR(100) NOT NULL,
    fact_text TEXT NOT NULL,             -- 事实内容
    category VARCHAR(50) DEFAULT 'working',  -- preference / summary / working / entity
    is_active BOOLEAN DEFAULT TRUE,      -- 软删除标记
    expires_at TIMESTAMPTZ,              -- 过期时间（NULL = 永不过期）
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_user_facts_user ON user_facts(user_id, is_active);
CREATE INDEX idx_user_facts_category ON user_facts(user_id, category, is_active);
```

### SQLAlchemy 模型

```python
from sqlalchemy import Column, String, Text, Float, Integer, ForeignKey
from sqlalchemy.dialects.postgresql import UUID
import uuid

from app.models.base import Base


class KnowledgeBase(Base):
    __tablename__ = "knowledge_base"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name = Column(String(200), nullable=False)
    description = Column(Text)
    created_at = Column(TIMESTAMPTZ, server_default=func.now())
    updated_at = Column(TIMESTAMPTZ, server_default=func.now(), onupdate=func.now())


class Document(Base):
    __tablename__ = "document"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    kb_id = Column(UUID(as_uuid=True), ForeignKey("knowledge_base.id", ondelete="CASCADE"))
    title = Column(String(500), nullable=False)
    file_path = Column(String(1000))
    doc_type = Column(String(20), default="md")
    content_text = Column(Text)
    content_hash = Column(String(64))
    status = Column(String(20), default="pending")
    chunk_count = Column(Integer, default=0)
    created_at = Column(TIMESTAMPTZ, server_default=func.now())
    updated_at = Column(TIMESTAMPTZ, server_default=func.now(), onupdate=func.now())
```

### OpenSearch 索引

OpenSearch 不需要预建表，首次写入时自动创建索引。但建议手动创建以指定中文分词器和 k-NN 配置（见 [3.4 节](#34-opensearch-向量存储实现)）。

---

## 部署方案

### Docker Compose 一键启动

```yaml
# docker-compose.yml
version: "3.8"

services:
  postgres:
    image: postgres:16-alpine
    environment:
      POSTGRES_DB: literag
      POSTGRES_USER: postgres
      POSTGRES_PASSWORD: postgres
    ports:
      - "5432:5432"
    volumes:
      - pg_data:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U postgres"]
      interval: 5s
      retries: 5

  redis:
    image: redis:7-alpine
    ports:
      - "6379:6379"
    volumes:
      - redis_data:/data

  opensearch:
    image: opensearchproject/opensearch:2.18.0
    environment:
      - discovery.type=single-node
      - plugins.security.disabled=true
      - "OPENSEARCH_JAVA_OPTS=-Xms512m -Xmx512m"
    ports:
      - "9200:9200"
    volumes:
      - os_data:/usr/share/opensearch/data

  api:
    build: ./backend
    ports:
      - "8000:8000"
    environment:
      - DATABASE_URL=postgresql+asyncpg://postgres:postgres@postgres:5432/literag
      - REDIS_URL=redis://redis:6379/0
      - OPENSEARCH_URL=http://opensearch:9200
      - OPENAI_API_KEY=${OPENAI_API_KEY}
      - COHERE_API_KEY=${COHERE_API_KEY}
    depends_on:
      postgres:
        condition: service_healthy
      opensearch:
        condition: service_started

volumes:
  pg_data:
  redis_data:
  os_data:
```

### 环境变量

```bash
# .env
DATABASE_URL=postgresql+asyncpg://postgres:postgres@localhost:5432/literag
REDIS_URL=redis://localhost:6379/0
OPENSEARCH_URL=http://localhost:9200
OPENAI_API_KEY=sk-...
COHERE_API_KEY=...              # 可选，没有则跳过重排
DOCLING_ENABLED=false           # 可选，不装 Docling 设 false
RERANKER_ENABLED=true           # 可选，设 false 跳过重排
```

### requirements.txt

```txt
fastapi>=0.115
uvicorn[standard]>=0.32
sqlalchemy[asyncio]>=2.0.36
asyncpg>=0.30
pydantic>=2.10
pydantic-settings>=2.6
openai>=1.50
httpx>=0.27
redis>=5.2
structlog>=24.4
pymupdf>=1.24
python-docx>=1.1
python-multipart>=0.0.12
langgraph>=0.2
# 可选
docling>=2.8
cohere>=5.13
```

### Dockerfile

```dockerfile
FROM python:3.12-slim

WORKDIR /app

# 系统依赖
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# Python 依赖
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 源码
COPY . .

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
```

### 启动步骤

```bash
# 1. 克隆项目
git clone https://github.com/yourname/LiteRAG.git
cd LiteRAG

# 2. 配置环境变量
cp .env.example .env
# 编辑 .env，填入你的 API Key

# 3. 启动服务
docker compose up -d

# 4. 等待 OpenSearch 初始化（约 30 秒）

# 5. 访问 API 文档
open http://localhost:8000/docs

# 6. 创建知识库
curl -X POST http://localhost:8000/api/v1/knowledge \
  -H "Content-Type: application/json" \
  -d '{"name": "公司制度", "description": "内部制度文档"}'

# 7. 上传文档
curl -X POST http://localhost:8000/api/v1/documents/upload \
  -F "file=@报销流程.pdf" \
  -F "kb_id=<上一步返回的 id>"

# 8. 提问
curl -N http://localhost:8000/api/v1/chat/stream \
  -H "Content-Type: application/json" \
  -d '{"query": "报销流程是什么？", "kb_ids": ["<id>"]}'
```

---

## 8 天学习路径

### Day 1：环境搭建 + 跑通

**学习内容**：
- 安装 Docker Desktop
- 理解 `docker-compose.yml` 中三个服务的作用
- `docker compose up -d` 启动三件套
- 阅读 `app/config.py`，理解 Pydantic Settings 配置管理
- 用 Swagger UI 调用 `/api/v1/health` 确认服务正常

**检查点**：
- [ ] 三个容器都在运行（`docker ps`）
- [ ] `http://localhost:8000/docs` 能打开
- [ ] `/api/v1/health` 返回 `{"status": "ok"}`

**目标**：三件套跑起来，API 文档能打开。

### Day 2：文档解析

**学习内容**：
- 阅读 `app/document/base.py`，理解 `DocumentParser` 抽象
- 实现 `MarkdownParser`（最简单，零依赖）
- 实现 `PDFParser`（用 pymupdf）
- 可选：安装 Docling，体验 AI 版面分析
- 用 `/api/v1/documents/upload` 上传一个 PDF，看解析结果

**检查点**：
- [ ] 能用代码解析一个 Markdown 文件并打印内容
- [ ] 能用代码解析一个 PDF 文件并打印内容
- [ ] 上传接口返回 doc_id

**目标**：能把 PDF / MD 解析成文本。

### Day 3：语义分块

**学习内容**：
- 阅读 `app/rag/chunker.py`，理解三种策略的优先级
- 实现 `_chunk_structural`（按 Markdown 标题分割）
- 实现 `_chunk_semantic`（TextTiling 算法）
- 实现 `_build_parent_child_index`（父子索引）
- 手动调用 `chunk_document(text)`，打印 chunks 观察分块效果

**检查点**：
- [ ] 结构化分块能正确提取 `title_path`
- [ ] TextTiling 分块能在话题边界处分割
- [ ] 父子索引能正确建立 `parent_id` 关联
- [ ] 能解释"为什么固定切分不好"

**目标**：理解为什么"固定切分"不好，TextTiling 为什么好，父子索引为什么让检索质量翻倍。

### Day 4：向量化 + 存储

**学习内容**：
- 阅读 `app/llm/embedder.py`，理解 `EmbeddingProvider` 抽象
- 实现 `OpenAIEmbedder`（调用 OpenAI API）
- 阅读 `app/rag/vector_store/base.py`，理解 `VectorStoreBase` 抽象
- 实现 `OpenSearchVectorStore`（upsert + search + fetch_by_ids）
- 上传文档后，在 OpenSearch Dashboard 查看索引数据

**检查点**：
- [ ] 能用 OpenAI API 把文本转向量
- [ ] 向量能写入 OpenSearch
- [ ] k-NN 检索能返回相似结果

**目标**：文档能向量化写入 OpenSearch，能用 k-NN 检索。

### Day 5：混合检索

**学习内容**：
- 阅读 `app/rag/retriever.py`
- 实现 `_vector_search`（k-NN 检索）
- 实现 `_fulltext_search`（BM25 检索）
- 实现 `_merge_and_dedupe`（合并去重）
- 实现 `_expand_to_parents`（父块回溯）
- 用 `/api/v1/search` 测试检索效果

**检查点**：
- [ ] 向量检索能找到语义相似的文档
- [ ] 全文检索能精确匹配关键词
- [ ] 合并后没有重复
- [ ] 父块回溯能返回完整上下文

**目标**：两路检索能跑通，父块回溯能把完整上下文带回来。

### Day 6：重排 + 生成

**学习内容**：
- 阅读 `app/rag/reranker.py`，实现 `CohereReranker`
- 阅读 `app/rag/generator.py`，实现 `Generator`
- 阅读 `app/rag/citation.py`，实现 `CitationExtractor`
- 用 `/api/v1/chat/stream` 测试完整流程
- 观察流式输出和引用标注

**检查点**：
- [ ] 重排后最相关的文档排在前面
- [ ] LLM 能流式输出答案
- [ ] 答案中有 [1] [2] 引用标注
- [ ] Context Cliff 降级日志正确触发

**目标**：端到端跑通——提问 → 检索 → 重排 → 生成 → 引用。

### Day 7：Agent Loop + 记忆引擎

**学习内容**：
- 安装 langgraph（`pip install langgraph`），理解 StateGraph 四要素：State / Node / Edge / 条件边
- 阅读 `app/agents/base.py`，理解 `_build_graph()` 如何把 think / execute / reflect 编排为状态图
- 理解 `add_conditional_edges` — reflect 的 `_should_retry()` 返回 "retry" 或 "done" 决定走哪条边
- 实现 `QAAgent`（继承 BaseAgent，实现 execute 方法）
- 阅读 `app/agents/registry.py`，理解注册表模式
- 阅读 `app/memory/memory_manager.py`，理解 L1 短期窗口 + L2 用户偏好
- 实现 `extract_and_save_facts()`（关键词启发式提取用户偏好）
- 用 `/api/v1/chat/stream` 测试多轮对话，观察记忆是否生效

**检查点**：
- [ ] 能画出 StateGraph 的节点和边（init → execute → reflect → 条件分支）
- [ ] Agent Loop 能流式输出答案
- [ ] `_should_retry` 检测到答案过短时返回 "retry"，触发重试
- [ ] 记忆引擎能加载 L1 短期窗口
- [ ] 记忆引擎能从 PostgreSQL 加载 L2 用户偏好
- [ ] 对话结束后用户偏好被保存到 user_facts 表
- [ ] 能解释 LangGraph 和 LangChain 的区别（运行时 vs 框架）

**目标**：理解 LangGraph StateGraph 如何编排 Agent Loop（think→execute→reflect 循环），记忆引擎如何让系统具备跨轮/跨会话记忆。

### Day 8：上下文工程 + 端到端串联

**学习内容**：
- 阅读 `app/context/focus_tracker.py`，理解焦点追踪规则
- 阅读 `app/context/coreference_resolver.py`，理解指代消解规则
- 在 QAAgent.execute() 中集成焦点追踪 + 指代消解
- 测试多轮对话场景：提问 → 追问省略句 → 验证消解效果
- 回顾整条链路，画一张包含 Agent Loop 的完整数据流图
- 尝试上传不同格式的文档（PDF / DOCX / MD）
- 调整 `top_k`、`chunk_size`、`max_iterations` 参数，观察效果变化
- 可选：用 Streamlit / Gradio 搭一个聊天界面
- 可选：写几个测试用例（参考 `tests/` 目录）

**检查点**：
- [ ] 焦点追踪能从对话历史提取话题和实体
- [ ] 指代消解能把"那上海呢？"补全为"上海的限号"
- [ ] 多轮对话中系统能理解省略句
- [ ] 能画出包含 Agent Loop 的完整数据流图
- [ ] 能解释 max_iterations 的作用
- [ ] 至少有 3 个测试用例通过

**目标**：理解上下文工程如何让系统理解省略句和指代，端到端串联 Agent Loop + 记忆引擎 + 上下文工程，理解智能体 RAG 的完整链路。

---

## 常见坑点

### 1. OpenSearch 内存不足

**现象**：OpenSearch 容器启动后立即退出，日志报 `OutOfMemoryError`。

**原因**：默认 JVM 堆内存太小。

**解决**：在 `docker-compose.yml` 中增加：

```yaml
environment:
  - "OPENSEARCH_JAVA_OPTS=-Xms1g -Xmx1g"
```

至少给 Docker 分配 4GB 内存。

### 2. IK 中文分词器未安装

**现象**：创建索引时报错 `analyzer [ik_max_word] not found`。

**原因**：OpenSearch 默认不带 IK 中文分词插件。

**解决**：进入 OpenSearch 容器安装插件：

```bash
docker exec -it <opensearch容器名> \
  ./bin/opensearch-plugin install \
  https://github.com/ik-analyzer/ik-analyzer/releases/download/v2.18.0/opensearch-analysis-ik-2.18.0.zip
```

英文环境可跳过，把 analyzer 改为 `standard`。

### 3. OpenAI API 超时

**现象**：文档上传后一直卡在 `embedding` 阶段。

**原因**：批量 embedding 请求太大或网络不稳定。

**解决**：减小 batch_size，加重试：

```python
# embedder.py 中分批处理
batch_size = 100  # 从 2048 降到 100
```

### 4. 向量维度不匹配

**现象**：OpenSearch 写入时报错 `vector dimension mismatch`。

**原因**：Embedding 模型维度与索引创建时的 `dimension` 不一致。

**解决**：确保 `OpenSearchVectorStore(dimension=3072)` 中的维度与 Embedder 的 `dim` 一致。切换模型时需要删除旧索引重建。

### 5. 父子索引 parent_id 为空

**现象**：检索结果中没有父块回溯，上下文太短。

**原因**：`_build_parent_child_index` 未被调用，或分块结果太少（< 2 块）。

**解决**：检查 `chunk_document` 的返回结果，确保每个子块都有 `parent_id`。文档太短时不会触发父子索引。

### 6. SSE 流式输出被缓冲

**现象**：前端收到的 SSE 数据是一整块，不是逐 token。

**原因**：Nginx 或反向代理缓冲了响应。

**解决**：在 Nginx 配置中关闭缓冲：

```nginx
proxy_buffering off;
proxy_cache off;
```

或直接用 `http://localhost:8000` 不经过代理。

### 7. Docker Compose 版本警告

**现象**：`docker compose up` 报 `version` 字段过时警告。

**原因**：Docker Compose V2 不再需要 `version` 字段。

**解决**：删除 `docker-compose.yml` 第一行的 `version: "3.8"`，不影响功能。

---

## 测试指南

### 测试策略

LiteRAG 建议为每个核心模块写单元测试。测试文件放在 `backend/tests/` 目录。

### 测试工具

```txt
# 测试依赖（加到 requirements.txt 或单独 requirements-dev.txt）
pytest>=8.0
pytest-asyncio>=0.24
httpx>=0.27  # 用于 API 测试
```

### 配置 pytest

```ini
# pytest.ini
[pytest]
asyncio_mode = auto
testpaths = tests
```

### 测试示例：分块器

```python
# tests/test_chunker.py
from app.rag.chunker import chunk_document, estimate_tokens


def test_estimate_tokens():
    assert estimate_tokens("") == 0
    assert estimate_tokens("hello") == 2  # 5 / 3.5 ≈ 1.4 → ceil → 2
    assert estimate_tokens("你好世界") == 2  # 4 / 3.5 ≈ 1.1 → ceil → 2


def test_structural_chunking():
    """结构化分块 — 按 Markdown 标题分割。"""
    text = """# 第一章

这是第一章的内容。

# 第二章

这是第二章的内容。"""
    chunks = chunk_document(text, doc_type="md", doc_id="test-1")
    assert len(chunks) >= 2
    assert "第一章" in chunks[0].content or "第一章" in chunks[0].title_path


def test_parent_child_index():
    """父子索引 — 子块有 parent_id。"""
    text = """# 标题一

段落一的内容，这是第一段。
段落二的内容，这是第二段。
段落三的内容，这是第三段。
段落四的内容，这是第四段。
段落五的内容，这是第五段。
段落六的内容，这是第六段。
段落七的内容，这是第七段。
段落八的内容，这是第八段。"""
    chunks = chunk_document(text, doc_type="md", doc_id="test-2")
    # 应该有子块带 parent_id
    children = [c for c in chunks if c.parent_id is not None]
    assert len(children) > 0
    # 父块不带 parent_id
    parents = [c for c in chunks if c.parent_id is None]
    assert len(parents) > 0
```

### 测试示例：检索器

```python
# tests/test_retriever.py
import pytest
from app.rag.retriever import HybridRetriever


@pytest.mark.asyncio
async def test_merge_and_dedupe():
    """合并去重 — 按 chunk_id 去重，保留高分。"""
    retriever = HybridRetriever()
    vector_results = [
        {"chunk_id": "a", "content": "doc A", "score": 0.9},
        {"chunk_id": "b", "content": "doc B", "score": 0.8},
    ]
    fulltext_results = [
        {"chunk_id": "a", "content": "doc A", "score": 0.7},  # 重复，分数低
        {"chunk_id": "c", "content": "doc C", "score": 0.6},
    ]
    merged = retriever._merge_and_dedupe(vector_results, fulltext_results)
    assert len(merged) == 3  # a, b, c
    # chunk "a" 应保留较高分 0.9
    a = next(r for r in merged if r["chunk_id"] == "a")
    assert a["score"] == 0.9


@pytest.mark.asyncio
async def test_graceful_degradation():
    """优雅降级 — 向量检索失败时返回空列表。"""
    retriever = HybridRetriever()
    # 不注入 embedder，模拟失败
    results = await retriever._vector_search("test", None, 10)
    assert results == []
```

### 运行测试

```bash
cd backend
pytest tests/ -v
```

---

## 与完整版的差异

### 完整版有但 LiteRAG 砍掉的能力

| 能力 | 完整版实现 | 为什么砍 |
|------|-----------|---------|
| 多租户隔离 | 四层防御（中间件 → DI → Repository → RLS） | 学习成本高，初学者不需要 |
| 知识图谱 | Neo4j + 规则 + LLM 三元组提取 + EntityRegistry | 需要额外部署 Neo4j，理解成本高 |
| 多模态 | 跨模态向量（jina-clip-v2）+ VLM 图片描述 + 图片节点 | 需要额外的 API 和模型，增加复杂度 |
| 视频 RAG | ffmpeg + ASR + 关键帧 VLM + 专用分块 | 需要 ffmpeg + GPU，环境搭建复杂 |
| 熔断器 | 三态状态机 + 自动恢复 | 生产级可靠性保障，学习版不需要 |
| Celery 异步任务 | Chord 并行编排 + 6 队列 + 死信队列 | 需要额外部署 RabbitMQ / Redis，增加复杂度 |
| MCP 工具协议 | 进程内 MCP Server + ContextVar 隔离 | 工具调用协议，超出 RAG 基础 |
| CrewAI 多 Agent | QA / Action / Workflow 三 Agent 协作 | 多 Agent 编排，适合进阶 |
| 企业连接器 | CRM / ERP / OA / Mail 适配器 | 企业集成，与 RAG 核心无关 |
| 协同编辑 | Yjs + Tiptap + WebSocket | 前端复杂度，与 RAG 无关 |
| 审批工作流 | HITL 三态守卫 + 会话级缓存 | 企业流程管控，与 RAG 无关 |
| 前端 | Astro 5 + React 19 + 34 页面 | 前端开发量大，建议用 Swagger UI |

### LiteRAG 保留的核心能力

| 能力 | 为什么保留 | LiteRAG 版 vs 完整版 |
|------|-----------|----------------------|
| 语义分块（TextTiling） | 这是 RAG 质量的基础，固定切分的检索效果差一个数量级 | 完整保留 |
| 父子索引 | Small-to-Big Retrieval 是让 LLM 理解上下文的关键设计 | 完整保留 |
| 混合检索（向量 + 全文） | 单路向量检索会漏掉精确匹配，BM25 补全召回 | 完整保留 |
| 重排器 | 让最相关的结果排到前面，直接影响用户体验 | 完整保留 |
| 流式生成 + 引用标注 | 这是 RAG 区别于"直接问 GPT"的核心价值 | 完整保留 |
| **Agent Loop** | 智能体的核心组件——让系统具备自我纠错能力 | 简化版：纯 Python while 循环、think 省略、reflect 简化、仅 QA Agent（LangGraph 为可选声明式路径） |
| **记忆引擎** | 智能体的核心组件——让系统记住用户偏好和对话上下文 | 简化版：两级记忆（L1+L2）、关键词提取、SQL 检索 |
| **上下文工程** | 智能体的核心组件——让系统理解省略句和指代 | 简化版：规则版焦点追踪 + 规则版指代消解 |
| 抽象基类 + 工厂模式 | 让代码可扩展、可测试、可替换 | 完整保留 |
| 优雅降级 | 即使依赖服务挂了，系统也能返回有意义的结果 | 完整保留 |
| Docker 一键部署 | 降低环境搭建门槛，8 天内跑通 | 完整保留 |

---

## 进阶路线图

学完 LiteRAG 后，如果想继续深入，以下是建议的进阶路径：

### 阶段 1：检索增强（1-2 周）

- 加入查询改写（`query_rewriter.py`）— 把用户的口语化提问改写为检索友好的关键词
- 加入实体识别同义词扩展 — "报销" 自动扩展 "费用申请"
- 加入 FAQ 匹配器 — 常见问题直接命中，跳过 LLM 生成

### 阶段 2：知识图谱（2-3 周）

- 部署 Neo4j
- 实现规则 + LLM 混合三元组提取
- 实现 EntityRegistry 实体归一化
- 图谱召回作为检索第三路
- 关联推荐（"看了这篇的人还看了..."）

### 阶段 3：多模态（2-3 周）

- 部署 VLM 服务（Pixtral 或 Qwen2.5-VL）
- 图片提取 + VLM 描述生成
- 跨模态向量检索（jina-clip-v2）
- 图片描述作为独立 chunk 向量化

### 阶段 4：Agent + 记忆进阶（3-4 周）

LiteRAG 已包含简化版 Agent Loop、记忆引擎、上下文工程。进阶方向：

- think() 接入 LLM 决策（检索 vs 生成 vs 工具调用）
- 新增 ActionAgent（执行型操作）+ WorkflowAgent（流程引导）
- LangGraph Checkpointer 持久化（PostgreSQL，支持中断恢复）
- LangGraph Human-in-the-Loop（敏感操作中断等待审批）
- 记忆引擎补齐 L2 检查点 + L4 工作记忆 + Embedding 语义检索
- 上下文工程补齐漂移检测 + 矛盾检测 + 对话摘要
- MCP 工具协议（让 Agent 调用外部系统）
- CrewAI 多 Agent 协作（复杂任务拆分）

### 阶段 5：生产级工程化（2-3 周）

- Celery 异步任务编排
- 熔断器 + 三层重试
- 多租户隔离（PostgreSQL RLS）
- 限流（Redis 令牌桶）
- 结构化日志 + LangFuse 追踪

---

## FAQ

### Q: 为什么不用 LangChain？

LangChain 是一个优秀的编排框架，适合快速搭建原型。但它的抽象层次太厚——你写的是 `chain = RetrievalQA.from_chain_type(...)`，看不到检索、重排、生成的细节。LiteRAG 的目标是让你理解每一步在做什么，所以 RAG 核心层（chunker、retriever、reranker、generator）全部手写，不套 LangChain。

### Q: 那为什么用 LangGraph？

LangChain 和 LangGraph 虽然同属 LangChain 生态，但定位完全不同。LangChain 是**框架**（厚抽象，隐藏 RAG 细节），LangGraph 是**运行时**（薄抽象，只管状态图编排）。用 LangGraph 编排 Agent Loop 时，你的检索、重排、生成逻辑仍然是手写代码，只是作为图的节点函数被调用——每一行都看得见。

LangGraph v1.0 LTS 于 2025 年 10 月正式发布 [$TRAE_REF](http://m.toutiao.com/group/7658595970690662962/)，LinkedIn、Uber、Klarna、J.P. Morgan 都在生产环境使用 [$TRAE_REF](http://m.toutiao.com/group/7658595970690662962/)。同时 LangChain 的经典 `AgentExecutor` 已被官方弃用 [$TRAE_REF](http://m.toutiao.com/group/7658595970690662962/)，推荐迁移到 LangGraph。LiteRAG 用 LangGraph 是顺应行业趋势，也让你学到的技能直接可用于生产环境。

一句话总结：**RAG 核心层不用任何框架（手写每一行），Agent Loop 层用 LangGraph（状态图编排是它的本职）**。

### Q: 为什么不用 Milvus？

OpenSearch 同时支持 BM25 全文检索和 k-NN 向量检索，一个引擎搞定两件事，运维简单。Milvus 是专用向量数据库，性能更好，但需要额外部署 etcd + minio，环境更复杂。

LiteRAG 的向量存储层是抽象的（`VectorStoreBase`），数据量超过 500 万向量时，注册一个 `MilvusVectorStore` 工厂函数即可切换，调用方代码零修改。

### Q: 不装 Docling 能用吗？

能。Docling 是可选的，设 `DOCLING_ENABLED=false` 即可。系统会降级到 pymupdf（PDF）和 python-docx（DOCX），处理基础文档没问题。Docling 的优势在于复杂版面（多栏、表格、扫描件），如果你的文档结构简单，降级方案够用。

### Q: 没有 Cohere API Key 能用吗？

能。设 `RERANKER_ENABLED=false` 跳过重排层。检索结果会按向量相似度分数排序，质量略低于重排后的结果，但系统功能完整。你也可以接入免费的本地重排模型（如 BGE-reranker-v2-m3），通过 TEI 部署。

### Q: 支持中文吗？

支持。OpenSearch 安装 IK 中文分词插件后，BM25 全文检索对中文的切词效果很好。Embedding 模型（text-embedding-3-large）原生支持中文。LLM 生成答案时，系统 prompt 指定用中文回答即可。

### Q: 能部署到生产吗？

LiteRAG 可以部署到小规模生产环境（日活 < 1000）。但缺少完整版的以下保障：

- 没有熔断器（外部 API 故障会导致请求堆积）
- 没有 Celery（大文档处理会阻塞请求）
- 没有多租户隔离（只适合单组织使用）
- 没有限流（可能被恶意请求打爆）

建议生产环境使用完整版，或在此基础上补齐这些能力。

### Q: 和 Dify / FastGPT 有什么区别？

Dify 和 FastGPT 是成熟的开源 RAG 平台，开箱即用，适合非技术人员。LiteRAG 是教学项目，目标是让你理解 RAG 的每个环节为什么这样设计。理解了 LiteRAG，你就有能力评估和定制 Dify / FastGPT 的实现，甚至自己造一个。

核心区别在于父子索引和 TextTiling 语义分块——Dify / FastGPT 大多使用固定长度切分，检索质量受限于切分策略。LiteRAG 的语义分块 + 父子回溯在复杂文档上的检索质量明显更好。

### Q: 为什么用 OpenSearch 而不是 Elasticsearch？

两者功能相似。选择 OpenSearch 的原因：

1. Apache 2.0 开源协议（Elasticsearch 改为 SSPL + Elastic License）
2. k-NN 插件性能稳定
3. 与 Elasticsearch API 兼容，迁移成本低

如果你已经部署了 Elasticsearch，把代码中的 `opensearch` 换成 `elasticsearch` 即可，API 几乎完全兼容。

---

## 开源协议与课程说明

### 开源协议

MIT License — 你可以自由使用、修改、分发、商用。

### 课程价值

这份文档对应的源码是 [EnterpriseKnowledge](https://github.com/jamesfeng2009/KnowledgeBase) 完整项目的精简版。完整版包含多租户、知识图谱、多模态、Agent、记忆引擎、Celery 编排、2296 个测试用例等企业级能力。

如果你学完 LiteRAG 后想继续深入，完整版源码可以作为进阶参考实现。完整版不是教学玩具——它是经过测试的生产系统，每一行代码都有对应的测试用例验证。

**进阶课程内容**：
- 知识图谱 + 多模态 + Agent Loop 实战（30-40 小时）
- 企业级工程化：多租户、熔断、Celery、MCP（50-60 小时）

### 学习建议

1. **动手写代码** — 不要只看文档，每学一个模块就自己实现一遍
2. **调试观察** — 用 `print` 或断点观察每一步的输入输出
3. **上传真实文档** — 用你公司的真实文档测试，比用示例文档学到的多
4. **调参感受** — 改 `top_k`、`chunk_size`、`max_iterations`，观察检索质量和 Agent 行为变化
5. **多轮对话测试** — 重点测试 Agent Loop 的反思重试、记忆引擎的偏好提取、上下文工程的指代消解，这是智能体 RAG 和普通 RAG 的核心区别
6. **写测试** — 为每个模块写 2-3 个测试用例，巩固理解

关注项目仓库获取更新通知。
