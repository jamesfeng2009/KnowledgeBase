# 检索服务 API 接口文档

> 版本：v2.3.0 ｜ 更新日期：2026-07-30 ｜ 基础路径：`/api/v2`

本文件描述 EnterpriseKB 检索服务对外暴露的全部 HTTP 接口，供前端、第三方系统及插件调用。

## 1. 接口概览

| 方法 | 路径 | 说明 | 鉴权 |
| --- | --- | --- | --- |
| POST | /retrieve | 混合检索 | Bearer Token |
| POST | /ask | 检索增强问答 | Bearer Token |
| GET | /documents/{id} | 获取文档详情 | Bearer Token |
| POST | /documents | 上传文档 | Bearer Token |
| DELETE | /documents/{id} | 删除文档 | Admin |
| GET | /health | 健康检查 | 无 |

## 2. 通用约定

### 2.1 请求头

```http
Content-Type: application/json
Authorization: Bearer <access_token>
X-Tenant-Id: tenant_001
```

### 2.2 统一响应结构

```json
{
  "code": 0,
  "message": "ok",
  "request_id": "req_8f3a2c",
  "data": {}
}
```

## 3. 接口详情

### 3.1 混合检索 `/retrieve`

向知识库发起一次检索，返回与查询最相关的文档片段。

#### 请求参数

| 参数 | 类型 | 必填 | 默认 | 说明 |
| --- | --- | --- | --- | --- |
| query | string | 是 | - | 用户查询文本 |
| top_k | int | 否 | 5 | 返回片段数量 |
| filters | object | 否 | {} | 元数据过滤条件 |
| rerank | bool | 否 | true | 是否启用重排序 |
| score_threshold | float | 否 | 0.35 | 最低相似度阈值 |

#### 请求示例

```bash
curl -X POST https://kb.example.com/api/v2/retrieve \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "query": "年假申请流程",
    "top_k": 5,
    "filters": {"department": "hr"},
    "rerank": true
  }'
```

#### 响应示例

```json
{
  "code": 0,
  "message": "ok",
  "request_id": "req_8f3a2c",
  "data": {
    "total": 5,
    "items": [
      {
        "id": "chunk_102",
        "doc_id": "doc_17",
        "title": "员工手册-假期管理",
        "content": "员工申请年假需提前在 OA 系统提交……",
        "score": 0.8721,
        "metadata": {"department": "hr", "page": 12}
      }
    ]
  }
}
```

### 3.2 检索增强问答 `/ask`

在检索结果基础上调用 LLM 生成自然语言回答，并附上引用来源。

#### 请求体

```json
{
  "query": "差旅报销需要哪些单据？",
  "conversation_id": "conv_55",
  "stream": true,
  "options": {"temperature": 0.2, "max_tokens": 800}
}
```

#### SSE 流式响应

```
event: token
data: {"delta": "差旅报销"}

event: token
data: {"delta": "需提供发票……"}

event: sources
data: {"refs": [{"doc_id": "doc_33", "page": 4}]}

event: done
data: {"usage": {"total_tokens": 612}}
```

## 4. 错误码

| code | HTTP 状态 | 含义 | 处理建议 |
| --- | --- | --- | --- |
| 0 | 200 | 成功 | - |
| 1001 | 400 | 参数缺失 | 检查请求体 |
| 1002 | 400 | 参数格式错误 | 校验类型 |
| 2001 | 401 | 未授权或 Token 过期 | 重新登录 |
| 2003 | 403 | 无权限访问该租户 | 申请授权 |
| 3001 | 404 | 资源不存在 | 核对 ID |
| 4001 | 429 | 触发限流 | 降低频率或升级配额 |
| 5000 | 500 | 服务内部错误 | 联系运维并提供 request_id |
| 5001 | 503 | 模型服务不可用 | 稍后重试或切换模型 |

## 5. 限流策略

- 默认每个租户 60 次/分钟
- 问答接口并发上限 10
- 超出限制返回 `429`，响应头包含 `X-RateLimit-Reset`

## 6. SDK 调用示例

```python
from enterprise_kb import KBClient

client = KBClient(base_url="https://kb.example.com", token=os.environ["TOKEN"])
result = client.retrieve(query="报销流程", top_k=3)
for item in result.items:
    print(item.title, item.score)
```

## 7. 版本兼容性

- v2.x 保持向后兼容，废弃接口会标注 `Deprecated` 并保留 2 个大版本
- 字段新增不会破坏旧客户端
- 字段删除需提前一个版本在响应中返回迁移提示

> 如需申请更高配额，请联系平台管理员并在工单中说明业务场景。
