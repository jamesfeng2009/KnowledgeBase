# Docker Compose 部署指南

> 本指南面向运维人员，介绍如何使用 Docker Compose 一键部署 EnterpriseKB 全套服务。

## 1. 部署架构概览

使用 Compose 编排以下服务：

1. `ekb-api`：后端 API 服务
2. `ekb-worker`：异步任务（文档解析、索引构建）
3. `postgres`：主数据库（含 pgvector）
4. `redis`：缓存与任务队列
5. `nginx`：反向代理与静态资源
6. `minio`：对象存储（可选）

## 2. 环境要求

| 资源 | 最低配置 | 推荐配置 |
| --- | --- | --- |
| CPU | 4 核 | 8 核 |
| 内存 | 8 GB | 16 GB |
| 磁盘 | 50 GB SSD | 200 GB SSD |
| 操作系统 | Linux x86_64 | Ubuntu 22.04 |

## 3. 准备工作

### 3.1 安装 Docker 与 Compose

```bash
curl -fsSL https://get.docker.com | sh
sudo apt-get install -y docker-compose-plugin
docker compose version
```

### 3.2 获取部署包

```bash
wget https://github.com/your-org/enterprise-kb/releases/download/v2.3.0/ekb-deploy.tar.gz
tar -xzf ekb-deploy.tar.gz
cd ekb-deploy
```

## 4. 配置说明

### 4.1 环境变量文件

复制并编辑 `.env`：

```bash
cp .env.example .env
```

```dotenv
POSTGRES_PASSWORD=strong-pwd-123
REDIS_PASSWORD=redis-pwd-456
EKB_SECRET_KEY=generate-a-random-key
EMBEDDING_API_KEY=sk-xxxx
LLM_API_KEY=sk-xxxx
EKB_PUBLIC_URL=https://kb.company.com
```

### 4.2 docker-compose.yml 关键片段

```yaml
services:
  ekb-api:
    image: ekb/api:2.3.0
    env_file: .env
    depends_on: [postgres, redis]
    ports: ["8000:8000"]
    deploy:
      replicas: 2
      resources:
        limits: {cpus: "2", memory: 2G}

  ekb-worker:
    image: ekb/worker:2.3.0
    env_file: .env
    depends_on: [postgres, redis]
    command: ["ekb", "worker", "--concurrency", "4"]

  postgres:
    image: pgvector/pgvector:pg16
    environment:
      POSTGRES_DB: enterprise_kb
      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD}
    volumes: ["pg-data:/var/lib/postgresql/data"]

volumes:
  pg-data:
```

## 5. 启动与停止

```bash
# 拉取镜像并启动全部服务（后台运行）
docker compose up -d

# 查看服务状态
docker compose ps

# 查看实时日志
docker compose logs -f ekb-api

# 停止服务
docker compose down

# 停止并清除数据卷（慎用）
docker compose down -v
```

## 6. 服务健康检查

启动后依次验证：

1. 数据库连通性：`docker compose exec postgres pg_isready`
2. Redis 连通性：`docker compose exec redis redis-cli ping`
3. API 健康：`curl http://localhost:8000/health`
4. 前端访问：浏览器打开 `http://localhost`

## 7. 数据持久化与备份

- 数据库数据卷 `pg-data` 必须纳入定期备份
- 建议每日执行 `pg_dump` 并上传至对象存储
- 对象存储（MinIO）数据卷单独挂载到大容量磁盘

备份脚本示例：

```bash
docker compose exec -T postgres pg_dump -U ekb enterprise_kb | gzip > backup_$(date +%F).sql.gz
```

## 8. 升级流程

```bash
# 1. 备份数据
./scripts/backup.sh

# 2. 拉取新镜像
docker compose pull

# 3. 滚动更新
docker compose up -d --no-deps ekb-api ekb-worker

# 4. 执行数据库迁移
docker compose exec ekb-api ekb db migrate
```

## 9. 故障排查

| 现象 | 可能原因 | 解决方法 |
| --- | --- | --- |
| 容器频繁重启 | 内存不足 | 调大 `resources.limits.memory` |
| API 返回 503 | Worker 未就绪 | 检查 Redis 连接 |
| 检索结果为空 | 索引未构建 | 执行 `ekb index rebuild` |
| 磁盘写满 | 日志/备份堆积 | 配置日志轮转并清理旧备份 |
| 时区错误 | 容器默认 UTC | 设置 `TZ=Asia/Shanghai` |

## 10. 安全建议

- 生产环境不要暴露数据库端口到公网
- 所有密码使用强随机值并定期轮换
- 启用 Nginx 的 HTTPS 与 HSTS

> 完整生产拓扑建议使用 Kubernetes，详见《CI-CD流水线说明》。
