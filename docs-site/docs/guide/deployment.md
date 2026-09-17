# 部署（Docker / K8s）

## Docker Compose

```bash
# 完整拓扑
docker compose up -d

# 私有部署（含 vLLM 模型服务）
DEPLOY_MODE=private_domestic docker compose --profile private up -d

# 启用 Qdrant 向量后端（可选）
docker compose --profile qdrant up -d
```

> 最小化部署见 `docker-compose.minimal.yml`（≤50 用户单机）。

## Kubernetes / Helm

```bash
helm install ekb infra/helm/enterprise-knowledge \
  --namespace ekb --create-namespace \
  --set image.tag=v1.0.0 \
  --set secrets.SECRET_KEY="$(openssl rand -hex 32)" \
  --set ingress.enabled=true --set ingress.host=kb.example.com
```

组件：core-engine（FastAPI）+ celery-worker/beat + postgres（pgvector）+
redis + rabbitmq + opensearch + minio（qdrant 可选）。

## 关键环境变量

| 变量 | 说明 |
| --- | --- |
| `DEPLOY_MODE` | saas / saas_dashscope / private_overseas / private_domestic / private_finetuned |
| `VECTOR_STORE` | os_knn / milvus / qdrant |
| `OBJECT_STORE` | local / s3（MinIO） |
| `BROKER_URL` | RabbitMQ（Celery） |
| `SECRETS_PROVIDER` | env / file / aws |
| `OIDC_ENABLED` | 开启 OIDC SSO |
| `WECOM_BOT_WEBHOOK` | 企业微信群机器人 |
