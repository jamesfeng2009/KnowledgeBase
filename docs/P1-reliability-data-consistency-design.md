# P1 可靠性与数据一致性设计方案

> 状态：待 Review | 创建日期：2026-07-21 | 作者：AI 助手

## 目录

- [多租户剩余待办实施建议](#多租户剩余待办实施建议)
- [P1-A：优雅关闭 + 指数退避+抖动 + 熔断器](#p1-a优雅关闭--指数退避抖动--熔断器)
- [P1-B：增量更新 + 内容哈希去重 + 幂等写入](#p1-b增量更新--内容哈希去重--幂等写入)
- [实施顺序与依赖关系](#实施顺序与依赖关系)

---

## 多租户剩余待办实施建议

### 现状总结

| 待办项 | 现状 | 改动量 | 风险 |
|--------|------|--------|------|
| AuditFlow 补 tenant_id | 模型无列、无迁移、RLS 未覆盖；Repository/Service/API 已预留参数位 | 极小（1 迁移 + 1 模型 + RLS 补充） | 低 |
| Celery tenant_id 传播 | 4/8 任务文件已支持参数；document_tasks 走间接隔离（从 doc.tenant_id 读取）已基本覆盖；video_tasks 有任务名 bug | 小（修 bug + 补传参） | 低 |
| 前端租户管理增强 | 基础功能完整（信息编辑/用量/模块门控）；缺失 SaaS 核心管理功能（创建租户/列表/用户分配/切换） | 大（前后端新增 API + UI） | 中 |

### 建议实施时机

**AuditFlow 补列 + Celery bug 修复：立即实施（与 P1 并行）**

理由：
- AuditFlow 补列改动极小（1 个 Alembic 迁移 + 模型加 1 行 + RLS 策略列表加 1 个表名），可在 30 分钟内完成
- Celery 发现两个 bug：`video_tasks.py:557` 调用不存在的任务名 `process_document_intelligence`（应为 `process_intelligence`）；`video_tasks.py` 中所有函数未注册为 Celery 任务却使用 `.s()` 签名调用
- 这两项不依赖 P1-A/P1-B，可并行推进

**前端租户管理增强：推迟到 SaaS 阶段**

理由：
- 当前是私有部署/开发阶段，单租户场景下创建租户、用户分配、租户切换等功能无实际使用场景
- `project_memory.md` 已记录决策："私有部署为主，SaaS 是远期可能 → 暂不做"
- 前端增强依赖后端新增 API（POST /tenants、GET /tenants 列表、用户分配端点），改动量大且当前无需求驱动

### 快速待办 Task（可立即执行）

| # | Task | 文件 | 预估 |
|---|------|------|------|
| MT-1 | AuditFlow 模型加 tenant_id 列 | `app/models/audit.py` | 5 min |
| MT-2 | Alembic 迁移：audit_flows 加 tenant_id + FK + 索引 | `alembic/versions/` 新文件 | 10 min |
| MT-3 | RLS 迁移补充 audit_flows 表 | `alembic/versions/2026_07_21_1700-*.py` TENANT_SCOPED_TABLES | 2 min |
| MT-4 | AuditRepository 去除"无法过滤"注释，激活过滤 | `app/repositories/audit_repository.py` | 5 min |
| MT-5 | AuditService.list_pending() 加 tenant_id 过滤 | `app/services/audit_service.py` | 5 min |
| MT-6 | 修复 video_tasks.py:557 任务名 bug | `tasks/video_tasks.py` | 2 min |
| MT-7 | 补充测试：AuditFlow tenant_id 过滤 | `tests/test_tenant_isolation_p2.py` | 10 min |

---

## P1-A：优雅关闭 + 指数退避+抖动 + 熔断器

### 1. 现状分析

#### 1.1 优雅关闭 — 严重缺失

| 组件 | 现状 | 问题 |
|------|------|------|
| FastAPI lifespan shutdown | 仅 1 行日志 `log.info("app.stopped")` | 无 engine.dispose()、无 Redis/HTTP 客户端关闭 |
| Signal handler | 完全缺失 | 无 SIGTERM/SIGINT 捕获 |
| Celery worker_shutdown | 无信号处理 | 关闭时不清理资源 |
| docker-compose stop_grace_period | 全部服务未配置 | Docker 强制 SIGKILL 前等待时间不足 |
| Dockerfile uvicorn | 无 `--timeout-graceful-shutdown` | uvicorn 收到 SIGTERM 后立即断开 |
| Chat SSE | 无心跳、无 CancelledError 处理 | 连接泄漏；违反"SSE 30s 心跳"约束 |
| 全局连接清理 | 7 个组件有 close()/aclose() 方法但无 shutdown 调用方 | 连接残留 |

**已实现的可靠性项**（保留）：
- Celery `task_acks_late=True` + `task_reject_on_worker_lost=True`
- Celery `worker_prefetch_multiplier=1`
- Celery `task_time_limit=1800` / `task_soft_time_limit=1500`
- Notification SSE 已有 30s 心跳 + CancelledError 处理

#### 1.2 指数退避重试 — 配置缺失

| 组件 | 现状 | 问题 |
|------|------|------|
| Celery 全局重试 | `task_default_retry_delay=60` 固定值 | 非指数退避、无 jitter |
| Celery @task 装饰器 | 7 个任务有 max_retries=3 但无 retry_backoff/retry_jitter | 手动 `raise self.retry(exc)` |
| 11 个任务 | 完全无重试逻辑 | notification/scheduled/intelligence/compounding/testing/multimodal |
| httpx.AsyncClient | 全部 `max_retries=0`（默认） | 外部 API 调用无重试 |
| LLM Provider | 依赖 SDK 默认重试（2 次） | 未显式配置 |
| 数据库操作 | 无重试 | OperationalError 直接抛出 |
| 重试库 | requirements.txt 无 tenacity/backoff | — |

**已实现的重试**（保留）：
- LLM SDK 内置指数退避 + jitter（Anthropic/OpenAI SDK 默认）
- RAG 质量守卫业务重试（`RAG_RETRIEVAL_MAX_RETRIES=1`，扩展 top_k）
- 死信队列机制（`_send_to_dead_letter`）

#### 1.3 熔断器 — 完全缺失

| 组件 | 现状 |
|------|------|
| 熔断器库 | 未引入（无 circuitbreaker/pybreaker/aiobreaker） |
| 熔断器实现 | 无 |
| 外部服务故障保护 | 仅有被动降级（fallback），无主动失败计数状态机 |
| 半开探测 | 无 |

**已实现的降级逻辑**（保留，与熔断器整合）：
- 限流器 Redis 不可用 → 内存模式
- RAG 引擎不可用 → PostgreSQL ILIKE
- Reranker 不可用 → 原始顺序
- Neo4j 不可用 → PG 查询
- VLM 不可用 → 跳过图片
- Celery 不可用 → 同步模式

### 2. 设计目标

1. **优雅关闭**：收到 SIGTERM 后，等待在途请求完成（≤30s），清理所有连接资源，零连接泄漏
2. **指数退避+抖动**：所有外部调用失败后按 `base * 2^n + random_jitter` 退避重试，避免雷群效应
3. **熔断器**：连续失败超阈值后快速失败（不等待超时），半开状态自动探测恢复，与现有降级整合

### 3. 架构设计

#### 3.1 优雅关闭流程

```
SIGTERM/SIGINT 到达
    ↓
uvicorn --timeout-graceful-shutdown=30s 开始倒计时
    ↓
FastAPI lifespan shutdown 执行：
    1. 拒绝新请求（uvicorn 自动处理）
    2. 等待在途请求完成（≤30s）
    3. 清理资源（按依赖逆序）：
       a. GraphService.close()          — Neo4j + Redis
       b. Reranker.aclose()             — httpx
       c. Embedder.aclose()             — httpx
       d. VectorStore.aclose()          — OpenSearch/Milvus httpx
       e. Retriever.aclose()            — httpx
       f. TokenCache.aclose()           — Redis
       g. NotificationHub.close()       — Redis PubSub
       h. RedisRateLimiter.close()      — Redis
       i. await engine.dispose()        — SQLAlchemy 连接池
    4. log.info("app.shutdown_complete")
    ↓
进程退出
```

Celery Worker 关闭流程：

```
SIGTERM 到达
    ↓
Celery worker_warm_shutdown 信号触发
    ↓
1. 停止接收新任务（worker_prefetch_multiplier=1 已保证）
2. 等待在途任务完成（task_acks_late=True 保证已执行任务被 ACK）
3. worker_shutdown 信号触发：
   a. 清理 Redis 连接
   b. 清理数据库连接池
4. 进程退出
```

#### 3.2 指数退避+抖动策略

```
重试间隔 = min(base * 2^attempt + random(0, base), max_delay)

参数：
  base = 1s（HTTP 调用）/ 5s（Celery 任务）/ 0.5s（DB 操作）
  max_delay = 60s
  max_retries = 3（默认）
  jitter = random(0, base)  — 全抖动模式

示例（base=1s, max_retries=3）：
  第1次重试：1 * 2^0 + rand(0,1) = 1~2s
  第2次重试：1 * 2^1 + rand(0,1) = 2~3s
  第3次重试：1 * 2^2 + rand(0,1) = 4~5s
```

三层重试体系：

| 层级 | 适用场景 | 实现 | 库 |
|------|---------|------|-----|
| L1 - HTTP 传输层 | httpx 外部 API 调用 | httpx-retry Transport | `httpx-retry` |
| L2 - 业务装饰器 | Service 层方法、DB 操作 | `@with_retry` 装饰器 | `tenacity` |
| L3 - Celery 任务 | 异步任务 | `@task` 装饰器参数 | Celery 内置 |

#### 3.3 熔断器状态机

```
    ┌─────────────────────────────────────────┐
    │                                         │
    ▼                                         │
┌─────────┐  连续失败 ≥ threshold  ┌─────────┐
│ CLOSED  │ ─────────────────────→ │  OPEN   │
│ (正常)   │                        │ (快速失败)│
└─────────┘ ←───────────────────── └─────────┘
    ▲    半开探测成功                    │
    │                                   │ 冷却时间 elapsed
    │                                   │ (recovery_timeout)
    │                                   ▼
    │                              ┌──────────┐
    └──────────────────────────────│ HALF_OPEN│
       探测请求成功                 │ (半开探测) │
                                   └──────────┘
                                        │
                                   探测请求失败
                                        │
                                        ▼
                                   回到 OPEN
```

参数：

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `failure_threshold` | 5 | 连续失败次数触发熔断 |
| `recovery_timeout` | 30s | OPEN → HALF_OPEN 冷却时间 |
| `half_open_max_calls` | 1 | 半开状态最多探测请求数 |
| `expected_exception` | Exception | 触发熔断的异常类型 |

### 4. 详细 Task

#### P1-A.1 优雅关闭 — FastAPI lifespan（预估 2h）

| # | Task | 文件 | 说明 |
|---|------|------|------|
| A1-1 | 创建 `app/utils/shutdown.py` 资源注册中心 | 新文件 | 单例 `ResourceManager`，register()/cleanup() 接口；各组件启动时注册 close 方法 |
| A1-2 | lifespan shutdown 增加资源清理 | `app/main.py` | shutdown 阶段调用 `await resource_manager.cleanup()`，按注册逆序清理 |
| A1-3 | 各组件注册清理函数 | `middleware.py`, `notification_hub.py`, `graph_service.py`, `reranker.py`, `embedder.py`, `vector_store/*.py`, `retriever.py`, `cache.py` | 在初始化时 `resource_manager.register(name, close_callable)` |
| A1-4 | Dockerfile 加 uvicorn shutdown 参数 | `backend/Dockerfile` | `--timeout-graceful-shutdown 30 --timeout-keep-alive 5` |
| A1-5 | docker-compose 加 stop_grace_period | `docker-compose.yml` | core-engine: 30s, celery-worker: 60s, 其他: 10s |

#### P1-A.2 优雅关闭 — Celery Worker（预估 1h）

| # | Task | 文件 | 说明 |
|---|------|------|------|
| A2-1 | Celery worker_shutdown 信号处理 | `celery_app.py` | `@signals.worker_shutdown.connect` 清理 Redis/DB 连接 |
| A2-2 | Celery worker_warm_shutdown 日志 | `celery_app.py` | 记录在途任务数、开始优雅关闭 |
| A2-3 | SoftTimeLimitExceeded 捕获 | `tasks/document_tasks.py`, `tasks/index_tasks.py` | 在关键任务中 `except SoftTimeLimitExceeded` 清理中间状态 |

#### P1-A.3 优雅关闭 — Chat SSE 心跳（预估 1h）

| # | Task | 文件 | 说明 |
|---|------|------|------|
| A3-1 | `sse_response()` 增加心跳生成器 | `app/utils/sse.py` | 每 30s yield `: heartbeat\n\n`，与 notification_hub 模式对齐 |
| A3-2 | Chat SSE CancelledError 处理 | `app/utils/sse.py` | 捕获 CancelledError，清理资源，发送 done 事件 |
| A3-3 | Chat SSE 超时自动关闭 | `app/utils/sse.py` | 长时间无 token 输出时自动关闭（防止僵尸连接） |

#### P1-A.4 指数退避 — 基础设施（预估 1.5h）

| # | Task | 文件 | 说明 |
|---|------|------|------|
| A4-1 | requirements.txt 加 tenacity + httpx-retry | `requirements.txt` | `tenacity>=8.2.0`, `httpx-retry>=0.4.0` |
| A4-2 | 创建 `app/utils/retry.py` | 新文件 | `@with_retry` 装饰器（tenacity AsyncRetrying）；`get_retry_transport()` 工厂（httpx-retry Transport） |
| A4-3 | 配置项 | `app/config.py` | `RETRY_BACKOFF_BASE`, `RETRY_BACKOFF_MAX`, `RETRY_MAX_ATTEMPTS`, `RETRY_JITTER` |

`app/utils/retry.py` 核心设计：

```python
import random
from tenacity import (
    AsyncRetrying, retry_if_exception_type,
    stop_after_attempt, wait_exponential_jitter,
    before_sleep_log, after_log,
)

def with_retry(
    max_attempts: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 60.0,
    jitter: float = 1.0,
    retry_exceptions: tuple = (Exception,),
):
    """指数退避+抖动重试装饰器。

    间隔公式：min(base * 2^(attempt-1) + random(0, jitter), max_delay)
    """
    def decorator(func):
        async def wrapper(*args, **kwargs):
            async for attempt in AsyncRetrying(
                stop=stop_after_attempt(max_attempts),
                wait=wait_exponential_jitter(initial=base_delay, max=max_delay, jitter=jitter),
                retry=retry_if_exception_type(retry_exceptions),
                before_sleep=before_sleep_log(log, logging.WARNING),
                after=after_log(log, logging.INFO),
                reraise=True,
            ):
                with attempt:
                    return await func(*args, **kwargs)
        return wrapper
    return decorator
```

#### P1-A.5 指数退避 — Celery 任务改造（预估 2h）

| # | Task | 文件 | 说明 |
|---|------|------|------|
| A5-1 | document_tasks 4 个任务加 retry_backoff | `tasks/document_tasks.py` | `@task(autoretry_for=(Exception,), retry_backoff=True, retry_backoff_max=60, retry_jitter=True, max_retries=3)` |
| A5-2 | index_tasks 3 个任务加 retry_backoff | `tasks/index_tasks.py` | 同上 |
| A5-3 | notification_tasks 2 个任务补充重试 | `tasks/notification_tasks.py` | 加 `@task(autoretry_for=(Exception,), retry_backoff=True, retry_jitter=True, max_retries=3)` |
| A5-4 | scheduled_tasks 5 个任务补充重试 | `tasks/scheduled_tasks.py` | 同上 |
| A5-5 | intelligence_tasks 1 个任务补充重试 | `tasks/intelligence_tasks.py` | 同上 |
| A5-6 | compounding_tasks 3 个任务补充重试 | `tasks/compounding_tasks.py` | 同上 |
| A5-7 | testing_tasks 3 个任务补充重试 | `tasks/testing_tasks.py` | 同上 |
| A5-8 | multimodal_tasks 1 个任务补充重试 | `tasks/multimodal_tasks.py` | 同上 |
| A5-9 | 移除手动 `raise self.retry(exc)` | `tasks/document_tasks.py` | autoretry_for 自动重试，手动 retry 逻辑改为 dead_letter 路由 |

#### P1-A.6 指数退避 — HTTP 客户端改造（预估 1.5h）

| # | Task | 文件 | 说明 |
|---|------|------|------|
| A6-1 | httpx.AsyncClient 加 retry Transport | `app/rag/reranker.py`, `app/llm/embedder.py`, `app/rag/retriever.py`, `app/rag/vector_store/*.py`, `app/asr/provider.py` | `transport=get_retry_transport(max_retries=3)` |
| A6-2 | LLM Provider 显式配置 max_retries | `app/llm/anthropic_provider.py`, `vllm_provider.py`, `dashscope_provider.py` | `max_retries=3`（覆盖 SDK 默认 2） |
| A6-3 | 数据库操作重试装饰器 | `app/repositories/base.py` | `@with_retry(retry_exceptions=(OperationalError,))` 用于关键写入 |

#### P1-A.7 熔断器实现（预估 3h）

| # | Task | 文件 | 说明 |
|---|------|------|------|
| A7-1 | 创建 `app/utils/circuit_breaker.py` | 新文件 | `CircuitBreaker` 类（closed/open/half-open 状态机）；`@circuit_breaker(name)` 装饰器；全局注册表 `circuit_registry` |
| A7-2 | 配置项 | `app/config.py` | `CIRCUIT_BREAKER_FAILURE_THRESHOLD=5`, `CIRCUIT_BREAKER_RECOVERY_TIMEOUT=30`, `CIRCUIT_BREAKER_HALF_OPEN_MAX_CALLS=1` |
| A7-3 | LLM Provider 熔断 | `app/llm/anthropic_provider.py`, `vllm_provider.py`, `dashscope_provider.py` | `@circuit_breaker("llm_api")` 包裹 chat() 方法；熔断时抛出 `CircuitBreakerOpenError` |
| A7-4 | 向量存储熔断 | `app/rag/vector_store/opensearch_store.py`, `milvus_store.py` | `@circuit_breaker("opensearch")`, `@circuit_breaker("milvus")`；熔断时降级 |
| A7-5 | Reranker 熔断 | `app/rag/reranker.py` | `@circuit_breaker("reranker")`；熔断时走 `_fallback` |
| A7-6 | TEI Embedder 熔断 | `app/llm/embedder.py` | `@circuit_breaker("embedder")`；熔断时降级 |
| A7-7 | 熔断器状态 API | `app/api/v1/settings.py` 或新文件 | `GET /api/v1/settings/circuit-breakers` 返回各熔断器状态（admin） |
| A7-8 | 熔断器状态日志 | `app/utils/circuit_breaker.py` | 状态转换时 `log.warning("circuit_breaker.state_change", ...)` |

`app/utils/circuit_breaker.py` 核心设计：

```python
import time
import asyncio
from enum import Enum
from functools import wraps

class CircuitState(Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"

class CircuitBreakerOpenError(Exception):
    """熔断器打开时抛出。"""
    pass

class CircuitBreaker:
    """异步熔断器 — closed/open/half-open 状态机。

    与现有降级逻辑整合：熔断器打开时抛出 CircuitBreakerOpenError，
    调用方捕获后走 fallback 路径。
    """
    def __init__(
        self,
        name: str,
        failure_threshold: int = 5,
        recovery_timeout: float = 30.0,
        half_open_max_calls: int = 1,
        expected_exception: type = Exception,
    ):
        self.name = name
        self._failure_threshold = failure_threshold
        self._recovery_timeout = recovery_timeout
        self._half_open_max_calls = half_open_max_calls
        self._expected_exception = expected_exception
        self._state = CircuitState.CLOSED
        self._failure_count = 0
        self._last_failure_time: float | None = None
        self._half_open_calls = 0
        self._lock = asyncio.Lock()

    async def call(self, func, *args, **kwargs):
        async with self._lock:
            await self._on_call()

        if self._state == CircuitState.OPEN:
            raise CircuitBreakerOpenError(
                f"Circuit breaker '{self.name}' is OPEN"
            )

        try:
            result = await func(*args, **kwargs)
            await self._on_success()
            return result
        except self._expected_exception as exc:
            await self._on_failure()
            raise

    async def _on_call(self):
        if self._state == CircuitState.OPEN:
            if self._last_failure_time and \
               time.monotonic() - self._last_failure_time >= self._recovery_timeout:
                self._state = CircuitState.HALF_OPEN
                self._half_open_calls = 0
                log.warning("circuit_breaker.half_open", name=self.name)

    async def _on_success(self):
        async with self._lock:
            self._failure_count = 0
            if self._state == CircuitState.HALF_OPEN:
                self._state = CircuitState.CLOSED
                log.info("circuit_breaker.recovered", name=self.name)

    async def _on_failure(self):
        async with self._lock:
            self._failure_count += 1
            self._last_failure_time = time.monotonic()
            if self._state == CircuitState.HALF_OPEN:
                self._state = CircuitState.OPEN
                log.warning("circuit_breaker.reopened", name=self.name)
            elif self._failure_count >= self._failure_threshold:
                self._state = CircuitState.OPEN
                log.warning("circuit_breaker.opened",
                           name=self.name, failures=self._failure_count)

# 全局注册表
circuit_registry: dict[str, CircuitBreaker] = {}

def get_circuit_breaker(name: str, **kwargs) -> CircuitBreaker:
    """获取或创建熔断器实例（单例）。"""
    if name not in circuit_registry:
        circuit_registry[name] = CircuitBreaker(name=name, **kwargs)
    return circuit_registry[name]
```

#### P1-A.8 测试（预估 2h）

| # | Task | 文件 | 说明 |
|---|------|------|------|
| A8-1 | 优雅关闭测试 | `tests/test_graceful_shutdown.py` | 验证 lifespan shutdown 调用所有注册的 close；验证 ResourceManager 逆序清理 |
| A8-2 | 重试装饰器测试 | `tests/test_retry.py` | 验证指数退避间隔、最大重试次数、jitter 范围、异常类型过滤 |
| A8-3 | 熔断器测试 | `tests/test_circuit_breaker.py` | 验证 closed→open→half_open→closed 状态转换；验证快速失败；验证半开探测 |
| A8-4 | SSE 心跳测试 | `tests/test_sse.py` | 验证 30s 心跳生成；验证 CancelledError 清理 |

### 5. 涉及文件清单

**新增文件**（4 个）：
- `app/utils/shutdown.py` — 资源注册中心
- `app/utils/retry.py` — 重试装饰器
- `app/utils/circuit_breaker.py` — 熔断器
- `tests/test_graceful_shutdown.py`, `tests/test_retry.py`, `tests/test_circuit_breaker.py`

**修改文件**（~25 个）：
- `app/main.py` — lifespan shutdown
- `app/config.py` — 配置项
- `app/middleware.py` — 注册 Redis 清理
- `app/utils/sse.py` — 心跳 + CancelledError
- `app/llm/*.py` — 熔断器 + max_retries
- `app/rag/reranker.py`, `retriever.py`, `cache.py` — 熔断器 + httpx retry
- `app/rag/vector_store/*.py` — 熔断器 + httpx retry
- `app/repositories/base.py` — DB 重试
- `celery_app.py` — worker_shutdown 信号
- `tasks/*.py`（8 个文件）— retry_backoff
- `Dockerfile` — uvicorn 参数
- `docker-compose.yml` — stop_grace_period
- `requirements.txt` — tenacity + httpx-retry

### 6. 风险评估

| 风险 | 概率 | 影响 | 缓解 |
|------|------|------|------|
| retry_backoff 改造后任务重试间隔变长 | 中 | 中 | max_retries=3 + retry_backoff_max=60s 限制总等待时间 |
| 熔断器误判导致正常请求被拒绝 | 低 | 高 | half_open 探测 + recovery_timeout=30s 自动恢复；阈值=5 避免偶发错误触发 |
| lifespan shutdown 超时导致强制杀死 | 低 | 低 | 30s 超时 + try/finally 保证关键清理 |
| httpx-retry 与 SDK 内置重试叠加 | 中 | 低 | LLM Provider 不加 httpx-retry（依赖 SDK）；仅对 TEI/ASR/Connector 加 |

---

## P1-B：增量更新 + 内容哈希去重 + 幂等写入

### 1. 现状分析

#### 1.1 增量更新 — 严重缺失

| 问题 | 现状 | 影响 |
|------|------|------|
| 更新文档不触发 reindex | `PUT /documents/{id}` 仅更新 DB，不触发 Celery | 索引/向量/图谱全部过期 |
| 重新处理不清理旧数据 | `_build_vector_index` 直接 upsert，不先 delete | 同 doc_id 累积多份 chunk |
| OpenSearch 索引无确定性 _id | `client.index()` 不传 id，自动生成 | 重复处理产生重复文档 |
| rebuild_kb_index 不删除旧索引 | 注释说"先删除"但代码未实现 | 重建产生重复 |
| Document 无 version 字段 | 无法追踪当前索引对应的版本 | 无法判断是否需要 reindex |
| doc.file_size 字段不存在 | `_should_use_multipart_pipeline` 访问不存在的属性 | GB 视频分流静默失效 |

#### 1.2 内容哈希去重 — 完全缺失

| 问题 | 现状 |
|------|------|
| Document 无 content_hash 列 | 模型和迁移均无 |
| 上传时不计算哈希 | `documents.py:177-212` 读取 bytes 后仅 UTF-8 解码 |
| 无重复文档检测 | `KnowledgeService.upload_document` 直接 create |
| chunk.id 随机 UUID | `uuid.uuid4()` 每次新随机，无法跨 run 去重 |
| 向量索引无去重 | upsert 理论幂等，但 chunk.id 变化导致等效 insert |

#### 1.3 幂等写入 — 基本缺失

| 问题 | 现状 |
|------|------|
| Celery 任务无幂等键 | `process_document` 可被多次投递，无锁无状态检查 |
| task_acks_late + reject_on_worker_lost | worker 崩溃后重投，可能重复执行 |
| chunk.id 随机 | upsert 等效 insert，产生孤儿数据 |
| OpenSearch 无 _id | 重复 index 产生重复文档 |
| DB 无 UPSERT | BaseRepository.create 用 add+flush |
| 向量索引不先 delete | 旧 chunk 成孤儿 |

**已具备的幂等能力**（保留）：
- Neo4j 图谱写入 MERGE 语义（`graph_service.py:1073, 1112`）
- 三元组提取内部去重（seen set，`graph_service.py:830`）
- Beat 单实例锁（`celery_app.py:175`）
- MinIO bucket 创建幂等
- 多段上传 abort 幂等

### 2. 设计目标

1. **增量更新**：文档内容变化时仅重建变化部分，reindex 前清理旧数据，保持索引与 DB 一致
2. **内容哈希去重**：上传时计算 SHA-256，同 KB 内重复内容直接返回已有文档，避免重复处理
3. **幂等写入**：Celery 任务基于 `doc_id + content_hash` 幂等，chunk.id 确定性生成，重复执行无副作用

### 3. 架构设计

#### 3.1 文档处理流水线改造

```
上传文档
    ↓
计算 content_hash = SHA-256(content_text)
计算 file_hash = SHA-256(file_bytes) [如有文件]
    ↓
查重：SELECT * FROM documents WHERE kb_id=? AND content_hash=? AND deleted_at IS NULL
    ├─ 已存在且 hash 相同 → 返回已有文档（幂等，跳过处理）
    └─ 不存在或 hash 变化 → 创建/更新文档
    ↓
process_document.delay(doc_id, content_hash)  ← 幂等键
    ↓
Celery 任务：
    1. Redis SETNX 锁：lock:process:{doc_id}:{content_hash}
       ├─ 已锁定 → 跳过（其他 worker 正在处理）
       └─ 获取锁 → 继续
    2. 加载 doc，检查 doc.indexed_content_hash == content_hash?
       ├─ 相同 → 跳过（已索引此版本）
       └─ 不同 → 继续 reindex
    3. 清理旧索引：
       a. vector_store.delete(doc_id)          — 删除旧向量
       b. opensearch delete_by_query(doc_id)   — 删除旧全文索引
       c. Neo4j 删除旧 MENTIONS 关系            — 删除旧图谱关系
    4. 重新分块（chunk.id = uuid5(doc_id + content_hash + index)）
    5. 构建新索引（向量 + 全文 + 图谱）
    6. 更新 doc.indexed_content_hash = content_hash
    7. 释放 Redis 锁
```

#### 3.2 chunk.id 确定性生成

```python
import uuid

# 基于 doc_id + content_hash + chunk_index 的确定性 UUID
DETERMINISTIC_NAMESPACE = uuid.UUID("00000000-0000-0000-0000-000000000000")

def deterministic_chunk_id(doc_id: str, content_hash: str, chunk_index: int) -> str:
    """生成确定性 chunk ID，保证同一文档同一内容的分块 ID 不变。

    这样 upsert 操作能真正覆盖旧数据，而非产生新记录。
    """
    return str(uuid.uuid5(
        DETERMINISTIC_NAMESPACE,
        f"{doc_id}:{content_hash}:{chunk_index}",
    ))
```

#### 3.3 文档版本追踪

```
Document 模型新增字段：
  content_hash: str | None      — 当前内容的 SHA-256
  file_hash: str | None         — 原始文件的 SHA-256（如有文件）
  file_size: int                — 文件大小（字节）
  indexed_content_hash: str | None  — 已索引内容的 SHA-256
  version: int = 1              — 文档版本号（每次内容更新 +1）

更新流程：
  content 变化 → content_hash 变化 → version += 1
  reindex 完成 → indexed_content_hash = content_hash
  判断是否需要 reindex：content_hash != indexed_content_hash
```

#### 3.4 幂等保护机制

```
三层幂等保护：

L1 - API 层查重：
  上传时 SELECT WHERE content_hash = ? → 已存在则返回

L2 - Celery 任务锁：
  Redis SETNX lock:process:{doc_id}:{content_hash} TTL=1800s
  获取失败 → 跳过（其他 worker 正在处理同一版本）

L3 - 索引版本检查：
  doc.indexed_content_hash == content_hash → 跳过（已索引此版本）
```

### 4. 详细 Task

#### P1-B.1 数据模型 + 迁移（预估 1.5h）

| # | Task | 文件 | 说明 |
|---|------|------|------|
| B1-1 | Document 模型加字段 | `app/models/knowledge.py` | `content_hash`, `file_hash`, `file_size`, `indexed_content_hash`, `version` |
| B1-2 | Alembic 迁移 | `alembic/versions/` 新文件 | `ALTER TABLE documents ADD COLUMN content_hash VARCHAR(64), file_hash VARCHAR(64), file_size BIGINT, indexed_content_hash VARCHAR(64), version INT DEFAULT 1` + 索引 `idx_documents_content_hash` |
| B1-3 | 回填现有数据 | 同迁移 | `UPDATE documents SET content_hash = md5(content_text), version = 1 WHERE content_text IS NOT NULL` |

#### P1-B.2 内容哈希计算 + 上传查重（预估 1.5h）

| # | Task | 文件 | 说明 |
|---|------|------|------|
| B2-1 | 创建 `app/utils/hash.py` | 新文件 | `compute_content_hash(text) -> str`（SHA-256）；`compute_file_hash(file_bytes) -> str` |
| B2-2 | 上传时计算哈希 | `app/api/v1/documents.py` `upload_document_file` | 读取 bytes 后计算 file_hash + content_hash；存入 Document |
| B2-3 | 上传前查重 | `app/services/knowledge_service.py` `upload_document` | `SELECT WHERE kb_id=? AND content_hash=? AND deleted_at IS NULL`；已存在则返回已有文档 |
| B2-4 | 导入时查重 | `app/api/v1/documents.py` `import_document_from_source` | 同上逻辑 |
| B2-5 | 查重 API 响应 | `app/schemas/document.py` | 返回 `{"duplicated": true, "existing_doc_id": "..."}` 或正常创建 |

`app/utils/hash.py` 核心设计：

```python
import hashlib

def compute_content_hash(text: str) -> str:
    """计算文本内容的 SHA-256 哈希。

    用于文档级去重和增量更新判断。
    """
    return hashlib.sha256(text.encode("utf-8")).hexdigest()

def compute_file_hash(file_bytes: bytes) -> str:
    """计算文件字节的 SHA-256 哈希。

    用于文件级去重（同一文件不同标题）。
    """
    return hashlib.sha256(file_bytes).hexdigest()
```

#### P1-B.3 chunk.id 确定性生成（预估 1h）

| # | Task | 文件 | 说明 |
|---|------|------|------|
| B3-1 | Chunk dataclass 加 content_hash 字段 | `app/rag/chunker.py` | `Chunk` 增加 `content_hash: str` 属性 |
| B3-2 | chunk.id 改为确定性生成 | `app/rag/chunker.py` | 所有 `str(uuid.uuid4())` 改为 `deterministic_chunk_id(doc_id, content_hash, index)` |
| B3-3 | 分块时传入 doc_id + content_hash | `app/rag/chunker.py` | `chunk()` 方法签名增加 `doc_id`, `content_hash` 参数 |

#### P1-B.4 增量更新 — reindex 触发（预估 1h）

| # | Task | 文件 | 说明 |
|---|------|------|------|
| B4-1 | 更新文档时计算新 content_hash | `app/api/v1/documents.py` `update_document` | 内容变化时 `new_hash = compute_content_hash(content_text)` |
| B4-2 | content_hash 变化时触发 reindex | `app/api/v1/documents.py` `update_document` | `if new_hash != doc.content_hash: process_document.delay(str(doc.id), new_hash)` |
| B4-3 | process_document 签名加 content_hash 参数 | `tasks/document_tasks.py` | `def process_document(self, doc_id: str, content_hash: str | None = None)` |
| B4-4 | version +1 逻辑 | `app/services/knowledge_service.py` | `update_document` 时 `doc.version += 1` |

#### P1-B.5 增量更新 — reindex 前清理（预估 2h）

| # | Task | 文件 | 说明 |
|---|------|------|------|
| B5-1 | 向量索引清理 | `tasks/document_tasks.py` `_build_vector_index` | upsert 前调用 `await store.delete(doc_id)` |
| B5-2 | OpenSearch 全文索引清理 | `tasks/document_tasks.py` `_build_opensearch_index` | index 前调用 `client.delete_by_query(index, {"query": {"term": {"doc_id": doc_id}}})` |
| B5-3 | OpenSearch 索引使用确定性 _id | `tasks/document_tasks.py` `_build_opensearch_index` | `client.index(index, id=f"{doc_id}:{chunk.id}", body={...})` |
| B5-4 | Neo4j 旧关系清理 | `tasks/document_tasks.py` `_build_knowledge_graph` | 建图前 `MATCH (d:Document {id: $doc_id})-[r:MENTIONS]->() DELETE r` |
| B5-5 | rebuild_kb_index 真正删除旧索引 | `tasks/index_tasks.py` `_rebuild_kb_index_async` | 循环前 `store.delete_kb(kb_id)` + OpenSearch delete_by_query |

#### P1-B.6 幂等写入 — Celery 任务锁（预估 1.5h）

| # | Task | 文件 | 说明 |
|---|------|------|------|
| B6-1 | 创建 `app/utils/task_lock.py` | 新文件 | `acquire_task_lock(key, ttl) -> bool`（Redis SETNX）；`release_task_lock(key)`；`task_lock(key, ttl)` 上下文管理器 |
| B6-2 | process_document 加幂等锁 | `tasks/document_tasks.py` | 任务开始时 `lock = acquire_task_lock(f"process:{doc_id}:{content_hash}", 1800)`；获取失败则跳过 |
| B6-3 | indexed_content_hash 版本检查 | `tasks/document_tasks.py` | `if doc.indexed_content_hash == content_hash: return`（已索引此版本） |
| B6-4 | reindex 完成后更新 indexed_content_hash | `tasks/document_tasks.py` `finalize_document_task` | `doc.indexed_content_hash = content_hash` |
| B6-5 | 释放锁（finally） | `tasks/document_tasks.py` | `finally: release_task_lock(lock_key)` |

`app/utils/task_lock.py` 核心设计：

```python
import asyncio
from contextlib import asynccontextmanager

@asynccontextmanager
async def task_lock(redis_client, key: str, ttl: int = 1800):
    """Celery 任务幂等锁 — Redis SETNX。

    获取失败表示同一任务已在其他 worker 执行，当前实例跳过。
    """
    acquired = await redis_client.set(key, "locked", nx=True, ex=ttl)
    if not acquired:
        log.info("task_lock.skipped", key=key)
        yield False
        return
    try:
        yield True
    finally:
        await redis_client.delete(key)
        log.info("task_lock.released", key=key)
```

#### P1-B.7 幂等写入 — 数据库层（预估 1h）

| # | Task | 文件 | 说明 |
|---|------|------|------|
| B7-1 | BaseRepository 增加 upsert 方法 | `app/repositories/base.py` | `async def upsert(self, model, unique_fields, values)` 使用 `INSERT ... ON CONFLICT ... DO UPDATE` |
| B7-2 | DocumentRepository 使用 upsert | `app/repositories/knowledge_repository.py` | `create_or_update_by_content_hash(kb_id, content_hash, ...)` |
| B7-3 | 知识资产 upsert | `app/repositories/` 相关 | compounding 相关表的幂等写入 |

#### P1-B.8 修复 doc.file_size（预估 0.5h）

| # | Task | 文件 | 说明 |
|---|------|------|------|
| B8-1 | 上传时记录 file_size | `app/api/v1/documents.py` | `doc.file_size = len(content_bytes)` |
| B8-2 | _should_use_multipart_pipeline 修复 | `tasks/document_tasks.py` | 确认 `doc.file_size` 存在后正常工作 |

#### P1-B.9 测试（预估 2.5h）

| # | Task | 文件 | 说明 |
|---|------|------|------|
| B9-1 | 哈希计算测试 | `tests/test_content_hash.py` | 验证 SHA-256 计算；相同内容相同哈希；不同内容不同哈希 |
| B9-2 | 上传查重测试 | `tests/test_upload_dedup.py` | 同 KB 同内容返回已有文档；不同 KB 允许重复；删除后可重新上传 |
| B9-3 | chunk.id 确定性测试 | `tests/test_chunker.py` | 同一文档同一内容分块两次，chunk.id 相同 |
| B9-4 | 幂等锁测试 | `tests/test_task_lock.py` | 同一 doc_id+content_hash 并发执行只处理一次 |
| B9-5 | reindex 清理测试 | `tests/test_reindex_cleanup.py` | reindex 后旧向量/全文/图谱数据被清理；无孤儿数据 |
| B9-6 | 增量更新测试 | `tests/test_incremental_update.py` | 更新文档触发 reindex；content_hash 不变时不触发；indexed_content_hash 正确更新 |

### 5. 涉及文件清单

**新增文件**（5 个）：
- `app/utils/hash.py` — 哈希计算
- `app/utils/task_lock.py` — 任务幂等锁
- `alembic/versions/` 新迁移文件
- `tests/test_content_hash.py`, `tests/test_upload_dedup.py`, `tests/test_chunker.py`, `tests/test_task_lock.py`, `tests/test_reindex_cleanup.py`, `tests/test_incremental_update.py`

**修改文件**（~12 个）：
- `app/models/knowledge.py` — Document 新字段
- `app/api/v1/documents.py` — 上传查重 + 更新触发 reindex
- `app/services/knowledge_service.py` — 查重逻辑 + version+1
- `app/rag/chunker.py` — 确定性 chunk.id
- `app/repositories/base.py` — upsert 方法
- `app/repositories/knowledge_repository.py` — upsert 查重
- `tasks/document_tasks.py` — 幂等锁 + 清理旧索引 + content_hash 参数
- `tasks/index_tasks.py` — rebuild 删除旧索引
- `app/schemas/document.py` — 查重响应
- `app/config.py` — 幂等锁 TTL 配置

### 6. 风险评估

| 风险 | 概率 | 影响 | 缓解 |
|------|------|------|------|
| 查重误判（不同文档相同内容） | 低 | 中 | 仅同 KB 内查重；返回 duplicated=true 让前端决策 |
| Redis 锁泄漏（worker 崩溃） | 中 | 中 | TTL=1800s 自动过期；finally 释放 |
| 确定性 chunk.id 冲突 | 极低 | 低 | uuid5 碰撞概率极低；doc_id+content_hash+index 三重保证 |
| reindex 清理误删 | 低 | 高 | 仅删除当前 doc_id 的数据；delete_by_query 带 doc_id 过滤 |
| 迁移回填大表慢 | 中 | 低 | 批量 UPDATE + 分批处理；开发阶段数据量小 |

---

## 实施顺序与依赖关系

### 推荐执行顺序

```
Phase 0：多租户快速待办（MT-1 ~ MT-7）     ← 立即开始，与 P1 并行
    ↓
Phase 1：P1-A 优雅关闭（A1 ~ A3）           ← 先做，不依赖 P1-B
Phase 1：P1-B 数据模型 + 迁移（B1）          ← 与 P1-A 并行
    ↓
Phase 2：P1-A 退避重试（A4 ~ A6）            ← 依赖 A4 工具模块
Phase 2：P1-B 哈希去重（B2 ~ B3）            ← 依赖 B1 迁移
    ↓
Phase 3：P1-A 熔断器（A7）                   ← 依赖 A4 工具模块
Phase 3：P1-B 增量更新（B4 ~ B5）            ← 依赖 B2 哈希
    ↓
Phase 4：P1-B 幂等写入（B6 ~ B8）            ← 依赖 B4 reindex 改造
    ↓
Phase 5：全部测试（A8 + B9）                 ← 各阶段测试可同步
```

### 依赖关系矩阵

| Task | 依赖 | 说明 |
|------|------|------|
| A5（Celery retry） | A4（retry.py） | 使用 retry_backoff 不依赖 retry.py，但保持一致性 |
| A7（熔断器） | A4（retry.py 可选） | 熔断器独立，但与重试配合使用 |
| B2（查重） | B1（迁移） | 需要 content_hash 列 |
| B3（chunk.id） | B2（hash.py） | 需要 content_hash 值 |
| B4（reindex 触发） | B1（迁移） | 需要 indexed_content_hash 列 |
| B5（清理旧索引） | B3（chunk.id） | 清理后用确定性 ID 重建 |
| B6（幂等锁） | B4（reindex 触发） | 锁保护 reindex 过程 |
| B7（DB upsert） | 无 | 独立 |

### 工作量预估

| 阶段 | P1-A | P1-B | 多租户 | 合计 |
|------|------|------|--------|------|
| Phase 0 | — | — | 40 min | 40 min |
| Phase 1 | 4h | 1.5h | — | 5.5h |
| Phase 2 | 5h | 2.5h | — | 7.5h |
| Phase 3 | 3h | 3h | — | 6h |
| Phase 4 | — | 3h | — | 3h |
| Phase 5 | 2h | 2.5h | 10 min | 4.5h |
| **合计** | **14h** | **15.5h** | **50 min** | **~30h** |

### 兼容性说明

- **无兼容期需求**：项目处于开发阶段，无历史数据，直接实施
- **向后兼容**：`content_hash` / `indexed_content_hash` 为 nullable，现有数据回填后正常工作
- **tenant_id=None 兜底**：与多租户隔离架构一致，私有部署不过滤
- **降级安全**：Redis 不可用时幂等锁降级为无锁（允许重复处理，由 indexed_content_hash 版本检查兜底）

---

## 附录：探索发现的关键 Bug

探索过程中发现以下既有 Bug（非本次 P1 引入，建议一并修复）：

| # | Bug | 位置 | 影响 | 修复方式 |
|---|-----|------|------|----------|
| BUG-1 | `video_tasks.py:557` 调用不存在的任务名 `process_document_intelligence` | `tasks/video_tasks.py:557` | 视频处理链 intelligence 步骤静默失败 | 改为 `process_intelligence.delay(doc_id)` |
| BUG-2 | `video_tasks.py` 所有函数未注册为 Celery 任务 | `tasks/video_tasks.py` | `.s()` 签名调用运行时失败 | 加 `@celery_app.task` 装饰器 |
| BUG-3 | `doc.file_size` 字段不存在 | `app/models/knowledge.py` | GB 视频分流判断永远 False | Document 模型加 `file_size` 字段（B1-1 已覆盖） |
| BUG-4 | `celery_app.py` include 缺少 3 个任务模块 | `celery_app.py:42-47` | intelligence/compounding/testing 任务可能不被发现 | include 列表补充 |
| BUG-5 | `rebuild_kb_index` 注释说删旧索引但代码未实现 | `tasks/index_tasks.py:276-318` | 重建产生重复数据 | B5-5 已覆盖 |

