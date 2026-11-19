# D10 资产出口接口契约（sources / citations）

2026-11-19 · 配套 D6 图片/表格资产化 · 后端已实现，前端渲染按本契约接入。

## 背景

D6 后图片/表格是独立检索 chunk（`chunk_type=image/table`）。聊天答案的
SSE 出口（`sources`、`citations` 事件）携带资产字段，前端据此把图表/图片
本身展示给用户，而不是只展示文字摘要。

## sources 事件

```
event: sources
data: {"sources": [
  {
    "id": "chunk-uuid",          // 命中的 chunk id（资产块即资产块自身 id）
    "title": "文档 > 章节",
    "content": "…",              // text/image: 截断 200 字符；table: 完整 HTML（不截断）
    "score": 0.9,
    "chunk_type": "text",        // text | image | table（新增，恒有）
    "asset_url": null            // 新增，恒有；image=presigned HTTP URL，其余 null
  }
]}
```

## citations 事件（答案含 [n] 标注时下发）

```
event: citations
data: {"citations": [
  {
    "citation_id": 1,
    "doc_id": "doc-uuid",
    "title": "…",
    "snippet": "…",              // text/image: 截断 200 字符+省略号；table: 完整 HTML
    "url": null,
    "chunk_type": "image",       // 新增，恒有
    "asset_url": "http://…签名…" // 新增，恒有；image=presigned URL，其余 null
  }
]}
```

## 前端渲染规则

| chunk_type | 渲染 |
|---|---|
| text | 现状不变（摘要文本） |
| image | `<img src=asset_url>`；asset_url 为 null 或加载失败时回退展示 content（VLM 文字描述） |
| table | 将 content/snippet 作为 HTML 渲染（完整 `<table>`，勿再截断） |

## 签名 URL 语义（重要）

- `asset_url` 在**下发时**签名（boto3 presigned GET，有效期
  `MINIO_PRESIGNED_EXPIRES_SECONDS`，默认 3600s）。存储层/向量库/答案缓存
  一律不存签名 URL，只存 `minio://` 规范地址。
- 签名失败时 `asset_url` 保留 `minio://bucket/key` 原址（不炸流）。
  前端对 `minio://` 开头的值按「不可直接访问」处理，走回退渲染。
- URL 过期后不可刷新复用：重新提问获取新 sources，或经文档详情端点
  （`GET /documents/{id}` 的 content_html 已含下发时签名）取新地址。
- 签名算法为 boto3 s3 默认 SigV2（`Signature=`/`Expires=` 参数），
  MinIO 接受；若未来迁移 AWS 新区域需升级 SigV4（见 task.md 勘误桶）。

## 答案正文契约

- 答案正文为纯文本/Markdown，不含资产 URL；正文中的 `[图片]`/`[表格]`
  标记来自源文档占位，前端无需特殊处理。
- 图表/图片的展示一律以 sources/citations 卡片为通道。

## 检索侧保证

- 资产块命中后不被父子回溯替换为父块正文（小块→大块扩展对 image/table 豁免）。
- BM25 全文路与向量路均携带 chunk_type/asset_ref。
- 快捷通道（shortcut）的 sources 事件尚未携带资产字段（形状不同，
  无 content），见 task.md D10 备注的后续项。