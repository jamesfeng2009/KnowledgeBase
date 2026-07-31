# 常见问题 FAQ

> 本文档汇总 EnterpriseKB 使用过程中高频出现的问题与解答，按类别组织。

## 1. 安装问题

### Q1：pip install 报错 "pgvector 构建失败"

**A**：需要先安装 PostgreSQL 开发头文件：

```bash
sudo apt-get install libpq-dev postgresql-server-dev-16
pip install pgvector
```

### Q2：启动时报 "CREATE EXTENSION vector 权限不足"

**A**：以超级用户连接数据库执行扩展创建，再将使用权限授予业务账户：

```sql
CREATE EXTENSION vector;
GRANT USAGE ON EXTENSION vector TO ekb;
```

### Q3：前端 npm run dev 端口被占用

**A**：修改 `web/vite.config.ts` 中的 `server.port`，或临时使用 `npm run dev -- --port 5174`。

## 2. 配置问题

### Q4：修改了 config/local.yaml 不生效

**A**：检查配置加载优先级。若设置了同名环境变量，会覆盖文件配置。可用 `ekb config show` 查看最终生效值。

### Q5：如何切换为本地 Embedding 模型

**A**：修改配置：

```yaml
embedding:
  provider: local
  model: bge-large-zh
  dim: 1024
```

并执行 `ekb index rebuild` 重建向量索引（维度变化必须重建）。

### Q6：多租户隔离模式如何切换

**A**：`tenant.isolation` 支持 `rls`/`schema`/`database` 三种。切换需停服并执行迁移脚本，生产环境谨慎操作，详见《多租户架构说明》。

## 3. 性能问题

### Q7：检索响应很慢（>3秒）

**A**：按以下顺序排查：

1. 检查向量索引是否建立：`SELECT indexname FROM pg_indexes WHERE tablename='document_chunks';`
2. 确认 HNSW 参数：`ef_search` 默认 40，可调到 80 提升召回但增耗时
3. 查看是否命中缓存：检查 Redis 监控
4. 数据量过大时考虑分片

### Q8：文档解析速度慢、队列堆积

**A**：

- 增加 Worker 实例数与并发数
- 关闭不必要的 OCR（扫描件才需要）
- 将大文件预处理拆分后上传

### Q9：LLM 问答经常超时

**A**：

- 降低 `llm.max_tokens`
- 配置 `llm.fallback_model` 降级到更快模型
- 检查到模型服务的网络延迟，必要时部署本地模型

## 4. 安全问题

### Q10：忘记管理员密码如何重置

**A**：在服务器执行：

```bash
ekb user reset-password --email admin@company.com
```

该命令需服务器本地访问权限，重置后会生成临时密码并要求首次登录修改。

### Q11：如何禁用某个用户的访问

**A**：将用户 `status` 设为 `disabled`：

```bash
ekb user disable --email someone@company.com
```

禁用后其 Token 立即失效。

### Q12：如何查看谁访问了某份敏感文档

**A**：查询审计日志：

```sql
SELECT user_id, action, created_at FROM audit_logs
WHERE resource_type='document' AND resource_id='doc_123'
ORDER BY created_at DESC;
```

## 5. 数据问题

### Q13：删除文档后检索仍能命中

**A**：删除是异步操作，分块清理有延迟。可手动触发：

```bash
ekb index cleanup --doc-id doc_123 --force
```

### Q14：向量化失败如何重试

**A**：文档状态为 `failed` 时，在管理后台点击「重试」，或：

```bash
ekb doc reindex --status failed --limit 100
```

### Q15：如何导出某租户全部数据

**A**：

```bash
ekb tenant export --id tenant_001 --out tenant_001.zip
```

导出包含文档、分块、配置，可用于迁移或备份。

## 6. 集成问题

### Q16：调用 /ask 接口返回 429

**A**：触发限流。可申请提升配额，或在客户端实现指数退避重试。

### Q17：SSE 流式响应在 Nginx 后被缓冲

**A**：在 Nginx 配置中关闭缓冲：

```nginx
location /api/v2/ask {
    proxy_buffering off;
    proxy_cache off;
    chunked_transfer_encoding on;
}
```

### Q18：飞书机器人如何接入

**A**：使用 Webhook 接收飞书事件，转发到 `/ask` 接口，再将回复通过飞书消息 API 发送。可参考插件市场的 `feishu-bot` 插件。

## 7. 升级问题

### Q19：升级后数据库迁移失败

**A**：

1. 查看迁移日志定位失败版本
2. 手动修复后执行 `ekb db stamp <version>` 标记
3. 重新 `ekb db upgrade`

### Q20：升级大版本需要重建索引吗

**A**：若向量维度未变，无需重建；若 Embedding 模型变更导致维度变化，必须重建。

> 未覆盖的问题请提交 Issue 或联系运维 on-call，附上 `request_id` 与日志片段。
