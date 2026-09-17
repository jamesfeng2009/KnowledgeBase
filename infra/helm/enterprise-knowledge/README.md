# =====================================================================
# Helm Chart — 企业知识库 Agent 平台（P1 平台工程补强）
# =====================================================================

## 前置条件

- Kubernetes 1.26+
- Helm 3.10+
- 集群支持 PVC（建议配置默认 StorageClass）

## 快速安装

```bash
# 1. 打包/直装（开发环境）
helm install ekb infra/helm/enterprise-knowledge --namespace ekb --create-namespace

# 2. 生产（覆盖镜像 tag 与密钥）
helm install ekb infra/helm/enterprise-knowledge \
  --namespace ekb --create-namespace \
  --set image.tag=v1.0.0 \
  --set secrets.SECRET_KEY="$(openssl rand -hex 32)" \
  --set secrets.POSTGRES_PASSWORD="$(openssl rand -hex 16)" \
  --set secrets.RABBITMQ_PASSWORD="$(openssl rand -hex 16)" \
  --set ingress.enabled=true \
  --set ingress.host=kb.example.com

# 3. 升级
helm upgrade ekb infra/helm/enterprise-knowledge -n ekb

# 4. 卸载
helm uninstall ekb -n ekb
```

## 组件

| 组件 | 默认启用 | 说明 |
| --- | --- | --- |
| core-engine | ✅ | 后端 API（FastAPI + Celery worker/beat 可选） |
| postgres | ✅ | pgvector 16，20Gi PVC |
| redis | ✅ | 缓存 + 任务结果 |
| rabbitmq | ✅ | Celery broker |
| opensearch | ✅ | 全文 + k-NN 向量 |
| minio | ✅ | 对象存储（S3 兼容） |
| qdrant | ❌ | 可选向量后端（`--set qdrant.enabled=true --set env.VECTOR_STORE=qdrant`） |

## 外部依赖接入

已有 PostgreSQL / RabbitMQ 时：

```bash
helm install ekb infra/helm/enterprise-knowledge \
  --set postgres.enabled=false \
  --set postgres.externalUrl="postgresql+asyncpg://user:pass@pg-host:5432/ekb" \
  --set redis.enabled=false \
  --set rabbitmq.enabled=false \
  --set minio.enabled=false
```

## 密钥说明

敏感配置统一放入 Secret（`SECRETS_PROVIDER=file` + `SECRETS_FILE_DIR=/run/secrets` 挂载）。
生产环境务必覆盖 `secrets.SECRET_KEY`（JWT/凭证加密主密钥），切勿使用默认值。
