# P0-P2 提升路线（对标 WeKnora）

本平台对照腾讯开源知识库 WeKnora（v0.8.0，Go 1.26 单体）的差距分析与
提升计划，详见仓库根目录 `WeKnora-差距分析与提升建议.md`。

## P0 —— 核心产品形态（已完成）

| 项 | 状态 | 说明 |
| --- | --- | --- |
| Wiki 模式 | ✅ | 自动生成互链 Markdown Wiki + 图谱 + 版本/回滚 |
| 分块编辑 + 版本历史 | ✅ | 分块可视化编辑、快照 diff、回滚、重建索引 |
| 文件夹树 | ✅ | 保留目录结构、重命名、归档、文档移动 |
| 数据源连接器框架 | ✅ | 飞书/企微/Notion/Confluence/Obsidian + 增量同步 |
| 对外 MCP Server | ✅ | StreamableHTTP + JSON-RPC 工具协议 + API Key 权限 |

## P1 —— 平台工程补强（已完成）

| 项 | 状态 | 说明 |
| --- | --- | --- |
| 模型厂商扩展 | ✅ | OpenAI 兼容协议一接入 20+ 厂商（OpenAI/DeepSeek/Moonshot/智谱/Groq/Ollama…） |
| 存储抽象层 | ✅ | 向量库：pgvector/os_knn/Milvus/Qdrant；对象存储：本地/S3（MinIO） |
| K8s/Helm 部署 | ✅ | `infra/helm/enterprise-knowledge` chart + Qdrant compose profile |
| IM 集成 | ✅ | 企业微信 connector + 群机器人 Webhook + 回调验签 |
| 凭证加密 AES-GCM + OIDC | ✅ | AES-GCM 已有；新增标准 OIDC 登录（discovery/授权/令牌交换） |
| 任务队列可视化面板 | ✅ | RabbitMQ 队列深度 + Celery Outbox 失败重试统计 |

## P2 —— 生态与体验（已完成）

| 项 | 状态 | 说明 |
| --- | --- | --- |
| i18n（英/日/韩） | ✅ | zh/en/ja/ko 资源 + Accept-Language 协商 + API |
| CLI + Chrome 剪藏插件 | ✅ | `scripts/ekb_cli.py` + `chrome-extension/`（MV3） |
| 网站嵌入 Widget | ✅ | Token 交换 + 限流 + 匿名问答 + 嵌入脚本 |
| 文档站（VitePress）+ 发布体系 | ✅ | `docs-site/` + `CHANGELOG.md` + 发布模板 |
| CI 强化 | ✅ | 12+ GitHub Actions workflow |

## 测试

```bash
cd backend
.venv/bin/python -m pytest tests/ -q
```

基线：4953+ passed；既有 13 个失败为本地基础设施未启动导致
（OpenSearch/Neo4j/Redis），与功能代码无关。
