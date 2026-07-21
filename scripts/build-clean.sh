#!/bin/bash
# 构建并清理 — 重建指定镜像后自动清理 dangling 镜像和过期构建缓存
# 用法: ./scripts/build-clean.sh [service1] [service2] ...
# 示例: ./scripts/build-clean.sh core-engine celery-worker
# 示例: ./scripts/build-clean.sh          # 重建所有服务

set -e

SERVICES="$@"

echo "==> [$(date '+%H:%M:%S')] 开始构建镜像..."
if [ -z "$SERVICES" ]; then
    docker compose build --parallel
else
    docker compose build $SERVICES
fi

echo ""
echo "==> [$(date '+%H:%M:%S')] 清理 dangling 镜像..."
DANGLING=$(docker images -f "dangling=true" -q)
if [ -n "$DANGLING" ]; then
    docker rmi $DANGLING 2>/dev/null || true
    echo "    已删除 $(echo "$DANGLING" | wc -l) 个 dangling 镜像"
else
    echo "    无 dangling 镜像"
fi

echo ""
echo "==> [$(date '+%H:%M:%S')] 清理 24 小时前的构建缓存..."
docker builder prune -f --filter "until=24h" 2>/dev/null || true

echo ""
echo "==> [$(date '+%H:%M:%S')] 当前磁盘占用:"
docker system df

echo ""
echo "==> 构建+清理完成"
