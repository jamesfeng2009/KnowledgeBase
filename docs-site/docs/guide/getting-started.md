# 快速开始

企业知识库 Agent 平台（EnterpriseKnowledge）是一个对标腾讯 WeKnora 的
企业级 RAG + Agent 知识库，提供 Wiki 模式、分块版本管理、文件夹树、
数据源连接器、对外 MCP Server 等能力。

## 环境要求

- Python 3.12+ / Node 18+ / Docker
- PostgreSQL（pgvector）/ OpenSearch / Redis / RabbitMQ（见 `docker-compose.yml`）

## 本地启动（后端）

```bash
cd backend
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env   # 按需配置 DEPLOY_MODE / 密钥
uvicorn app.main:app --reload --port 8000
```

## 本地启动（前端 Astro）

```bash
cd frontend
npm install && npm run dev
```

## 最小化部署

```bash
docker compose -f docker-compose.minimal.yml up -d
```

## 文档站

```bash
cd docs-site
npm install && npm run dev    # 本站点
```

## 下一步

- [架构总览](/guide/architecture)
- [P0-P2 提升路线](/guide/p0-p2-roadmap)
- [部署（Docker / K8s）](/guide/deployment)
