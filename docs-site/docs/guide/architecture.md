# 架构总览

## 分层

```
┌─────────────────────────────────────────────────────┐
│ 接入层   Astro 前端 · CLI · Chrome 插件 · Widget · MCP │
├─────────────────────────────────────────────────────┤
│ API 层   FastAPI /api/v1（文档·知识库·搜索·Wiki·        │
│          folders·chunks·i18n·widget·im·oidc·queues）  │
├─────────────────────────────────────────────────────┤
│ 服务层   Knowledge / Search / Wiki / Chunk / Folder / │
│          WeCom Bot / OIDC / Widget Token / Queue 监控 │
├─────────────────────────────────────────────────────┤
│ 模型层   Document · KB · WikiPage · DocumentChunk ·    │
│          KbFolder · TaskOutbox · ExternalCredential   │
├─────────────────────────────────────────────────────┤
│ 基础设施 PostgreSQL(pgvector) · OpenSearch · Milvus ·  │
│          Qdrant · Redis · RabbitMQ(Celery) · MinIO ·  │
│          Neo4j · vLLM/DashScope/Anthropic/OpenAI      │
└─────────────────────────────────────────────────────┘
```

## 关键路径

- **文档入库**：上传/剪藏 → Document → Celery 解析分块 → 向量索引
  （os_knn / Milvus / Qdrant）→ 可检索
- **检索**：全文（OpenSearch BM25）+ 向量（k-NN）+ 图谱（Neo4j，可选）
  四路召回 → 重排 → LLM 生成（多厂商 Provider）
- **Wiki**：LLM 逐文档生成 Markdown（`[[互链]]`）→ 幂等 upsert +
  版本快照 → 图谱节点/边
- **外部同步**：connector（飞书/企微/Notion/Confluence/Obsidian）
  → external_sync_service 增量拉取

## 安全

- 多租户隔离（RLS + tenant_id 下推）、ABAC 密级过滤、Final Gate
- 凭证 AES-GCM 加密存储；OIDC SSO；API Key 范围授权
- Widget 匿名令牌 HMAC 签名 + 限流；对象存储路径穿越防护
