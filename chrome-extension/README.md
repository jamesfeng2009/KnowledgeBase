# EKB 剪藏助手（Chrome 扩展）

把网页选中内容一键剪藏到企业知识库（配合 `POST /api/v1/documents/clip`）。

## 安装

1. 打开 `chrome://extensions`，开启「开发者模式」；
2. 点击「加载已解压的扩展程序」，选择本目录 `chrome-extension/`；
3. 点击扩展图标，填写 API 地址 / API Key / 知识库 ID 并保存。

## 使用

- 在任意网页选中文字 → 右键 →「剪藏到企业知识库」；
- 剪藏成功会弹出系统通知。

## 配置

| 字段 | 说明 |
| --- | --- |
| API 地址 | 后端地址，如 `http://localhost:8000` |
| API Key | 后端签发的 API Key（`/api/v1/apikeys`） |
| 知识库 ID | 目标知识库 UUID |

## 依赖后端接口

`POST /api/v1/documents/clip`

```json
{
  "kb_id": "uuid",
  "title": "网页标题",
  "content": "选中文本",
  "source_url": "https://example.com/page"
}
```

> 注：`icons/` 目录需放置 16/48/128 三档图标后方可发布；本地开发可直接使用
> 任意 PNG 占位图，或移除 `icons` 字段后加载（Chrome 会使用默认图标）。
