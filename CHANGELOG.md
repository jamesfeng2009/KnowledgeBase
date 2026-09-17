# Changelog

本项目遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.0.0/)，
版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [Unreleased]

### P2 生态与体验（完成）

- **i18n**：新增 zh / en / ja / ko 四语言资源、Accept-Language 协商与
  `/api/v1/i18n/*` 资源 API
- **CLI**：新增 `scripts/ekb_cli.py`（kb / doc upload / search / wiki / queues），
  客户端封装 `app/cli/client.py` 可复用
- **Chrome 剪藏插件**：新增 `chrome-extension/`（MV3），支持选中文本右键剪藏，
  配套后端 `POST /api/v1/documents/clip`
- **网站嵌入 Widget**：新增 `/api/v1/widget/token`（短期 HMAC 令牌）与
  `/api/v1/widget/ask`（限流 + 知识库全文检索），嵌入脚本
  `frontend/public/widget/ekb-widget.js`
- **文档站**：新增 `docs-site/`（VitePress）与 `CHANGELOG.md`、发布模板
  `RELEASE_TEMPLATE.md`
- **CI 强化**：新增 12+ GitHub Actions workflow（lint / 单测 / 前端类型检查 /
  迁移 / 安全扫描 / 镜像构建 / 发布等）

### P1 平台工程补强（完成）

- **模型厂商扩展**：新增 `OpenAIProvider`（OpenAI 兼容协议，一接入 20+ 厂商）
- **存储抽象层**：新增 `QdrantVectorStore`（`VECTOR_STORE=qdrant`）与
  对象存储抽象 `app/storage/`（本地 / S3·MinIO，`OBJECT_STORE`）
- **K8s/Helm**：新增 `infra/helm/enterprise-knowledge` chart；
  compose 新增 qdrant profile
- **IM 集成**：新增企业微信 `WeComConnector`、群机器人 Webhook 服务与
  `/api/v1/im/*` API（回调验签）
- **OIDC**：新增标准 OIDC 登录（discovery / 授权 / 令牌交换 / userinfo /
  用户落库），`/api/v1/auth/oidc/*`
- **任务队列面板**：新增 `/api/v1/observability/queues`（RabbitMQ 队列深度 +
  Celery Outbox 失败重试统计）

### P0 核心产品形态（完成）

- **Wiki 模式**：`WikiPage/WikiPageVersion/WikiPageLink` + LLM 自动生成
  互链 Markdown + 图谱 + 编辑/回滚
- **分块编辑 + 版本历史**：`DocumentChunk/ChunkVersion` + 编辑回滚 +
  索引重建
- **文件夹树**：`KbFolder` + 树形 API + 文档移动/重命名/归档

## [v1.0.0] - 2026-09-17

首个可部署版本（历史功能基线：混合检索 / 多租户 / MCP / 连接器 / 记忆 /
进化闭环等，详见 `git log`）。
