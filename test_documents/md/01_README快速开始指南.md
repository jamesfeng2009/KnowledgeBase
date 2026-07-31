# EnterpriseKB 快速开始指南

> 欢迎使用 EnterpriseKB —— 面向企业的私有化知识库与 RAG 检索增强生成平台。
> 本指南帮助你在 10 分钟内完成本地环境的搭建并跑通第一个检索请求。

## 1. 项目简介

EnterpriseKB 是一套开箱即用的企业级知识库解决方案，融合了文档解析、向量化检索、大语言模型问答与权限管理能力。它适用于内部文档检索、客服知识辅助、合规审计问答等场景。

### 1.1 核心能力

- 多格式文档解析（PDF / DOCX / Markdown / HTML / 纯文本）
- 混合检索（向量 + 关键词 BM25 + 重排序）
- 多租户与细粒度权限控制
- 可插拔的 LLM 与 Embedding 模型适配
- 完整的审计日志与访问追踪

### 1.2 适用人群

| 角色 | 关注点 |
| --- | --- |
| 运维工程师 | 部署、监控、扩容 |
| 后端开发者 | API 集成、插件开发 |
| 知识管理员 | 文档上传、权限配置 |
| 终端用户 | 提问与检索 |

## 2. 环境要求

在开始之前，请确认你的机器满足以下条件：

- Python 3.10 及以上
- Node.js 18 及以上（仅前端构建需要）
- PostgreSQL 14+（启用 pgvector 扩展）
- Redis 6+
- Docker 20.10+（可选，推荐容器化部署）

## 3. 安装步骤

### 3.1 克隆代码仓库

```bash
git clone https://github.com/your-org/enterprise-kb.git
cd enterprise-kb
```

### 3.2 创建虚拟环境并安装依赖

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pip install -e .
```

### 3.3 初始化配置

复制示例配置文件并按需修改：

```bash
cp config/example.yaml config/local.yaml
```

关键配置项示例：

```yaml
database:
  host: 127.0.0.1
  port: 5432
  name: enterprise_kb
  user: ekb
  password: "change-me"

embedding:
  provider: openai
  model: text-embedding-3-small
  api_key: ${EMBEDDING_API_KEY}

llm:
  provider: openai
  model: gpt-4o-mini
```

## 4. 快速启动命令

执行以下命令完成数据库迁移并启动服务：

```bash
# 初始化数据库表结构
ekb db init

# 导入示例知识库
ekb seed --path ./examples/sample-docs

# 启动后端 API 服务（默认 8000 端口）
ekb serve --host 0.0.0.0 --port 8000

# 另开终端启动前端
cd web && npm install && npm run dev
```

启动成功后访问 `http://localhost:5173` 即可看到前端界面。

## 5. 目录结构说明

```
enterprise-kb/
├── backend/             # 后端服务（FastAPI）
│   ├── api/             # 路由与接口定义
│   ├── core/            # 核心检索与索引逻辑
│   ├── models/          # 数据模型
│   └── plugins/         # 插件目录
├── web/                 # 前端工程（Astro + Vue）
├── config/              # 配置文件
├── docs/                # 项目文档
├── scripts/             # 运维与部署脚本
├── tests/               # 测试用例
└── requirements.txt
```

## 6. 验证安装

使用命令行客户端发起一次检索，确认服务可用：

```bash
ekb ask "公司差旅报销标准是什么？"
```

预期输出示例：

```
[来源: 行政管理手册.pdf 第3.2节]
差旅报销标准按职级划分，详见下表……
```

## 7. 下一步

- 阅读《API接口文档-检索服务》了解完整接口
- 参考《部署指南-Docker Compose》进行生产环境部署
- 查看《架构说明文档》理解系统设计

> 提示：如果遇到依赖冲突，建议使用 `poetry install` 替代 pip。

## 8. 常见问题速查

1. **端口被占用**：修改 `config/local.yaml` 中的 `server.port`。
2. **pgvector 未安装**：在数据库执行 `CREATE EXTENSION vector;`。
3. **Embedding 超时**：检查网络代理或切换本地模型。
