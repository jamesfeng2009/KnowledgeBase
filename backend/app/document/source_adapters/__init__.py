"""
文档来源适配器包 — 管理多平台文档的拉取与统一格式化。

支持平台（按优先级）：
    P0: Confluence（REST API → HTML）、Obsidian（本地 .md 文件）
    P1: 飞书 Wiki（OpenAPI 导出 DOCX）、Notion（blocks API → Markdown）
"""
