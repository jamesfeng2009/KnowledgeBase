# 稳定性与安全加固

## 稳定性与安全加固（P0-P3）

针对全链路代码审查发现的问题，按优先级分四批完成系统性修复，全部附带回归测试。

### P0 严重修复（16 项）

| 层面 | 修复项 |
|------|--------|
| 后端 | SSE 心跳协程异常隔离（心跳失败不再杀流）；熔断器半开状态卡死修复；后台任务锁经 `run_coroutine_threadsafe` 提交运行中事件循环；跨事件循环 Futures 混乱修复；L1/L2 缓存 key 加 `tenant_id` 堵跨租户泄漏；向量/全文/BM25 三路 `kb_id` 过滤对齐；OpenSearch 索引名错误；视频任务调用已删函数；错误文本不再写入缓存；文档越权读/改（owner 校验）；multipart 会话 IDOR（归属校验）；Beat 锁定时续约防过期 |
| 前端 | markdown 渲染 XSS 修复（raw HTML 转义白名单）；SSE 客户端两处 TCP 分包丢事件（跨 chunk 行缓冲）；`onDone` 双调幂等收口 |

### P1 高优修复

- `health_check` Redis 连接改复用单例（原每次新建连接且不关闭）；`check_all` 改并发执行 + 单 Provider 超时隔离
- `auth.ts` 仅 401 清除 Token——网络抖动/5xx 不再误登出
- ProviderHealthCard 补 XSS 转义、硬编码颜色换设计系统 CSS 变量、定时器页面切走自动清理

### P2 中优修复

| 层面 | 修复项 |
|------|--------|
| 后端 | 上传内存 DoS 两道闸门（Content-Length 预检 + 1MB 分块流式限流）；同步 redis 改 `redis.asyncio`；`/health/providers` 非 admin 脱敏（错误细节仅管理员可见）；SSE 流式期间释放 DB 连接回池（`prepare_chat` 前置读写）；admin 密级口径统一放行；权限异常转 SSE error 事件；chunk Redis 序列化补全 `token_count`/`start_pos`/`end_pos`；finalize failed 短路 + 重复 finalize 幂等；OpenSearch 写入确定性 `_id`（upsert 幂等） |
| 前端 | 流式中切换会话竞态修复（AbortController 先中止再切换）；流式渲染 80ms 节流（O(n²) → O(n)）；`CSS.escape` 防选择器注入；sse.ts 401 处理对齐 api.ts；通知 SSE 改 fetch + Authorization 头（Token 不再进 URL）；协同编辑器图片监听重复注册修复 |
| 熔断器契约统一 | 向量存储 `search` 经 `@circuit_call` 保护（异常传播记录失败、OPEN 快速拒绝、retriever 捕获降级）；新旧测试契约对齐；测试间熔断器状态 fixture 隔离 |

### P3 低优清理

- 68 处未使用 import 清理（ruff F401 + 人工复核）
- `TokenCache` L2 无界 dict → 有界 LRU（`CACHE_L2_MAX_SIZE`，默认 1000，TTL 语义保留）
- 三条失败降级路径补 Redis 孤儿 chunk 清理
- OpenSearch 索引逐 chunk HTTP → 单次 `bulk`（N 次往返 → 1 次）
- 前端 39 个文件统一 `escapeHtml` 五重转义（修 10 处 HTML 属性注入面）
- CircuitBreakerCard 颜色/XSS/定时器清理；头像渲染逻辑抽取复用

### 工程规范

- **幂等**：重复上传 / 重复 finalize / 重复审批 / 任务重试均不产生重复副作用（确定性 `_id` upsert、状态终态短路、原子计数器去重）
- **日志**：后端统一 `get_logger(__name__)` 结构化键值；前端统一 `console.log('[模块名] ...')` 前缀
- **测试**：本轮新增 145 项加固回归用例；全量 1969 项中 1908 通过，其余 55 项为既有环境基线（真实 PG 外键/数据残留、celery mock 污染等），零新增失败

---