"""外部凭证 Schema — P0+P3 外部文档实时同步的凭证管理。

单一职责：入参校验与出参序列化，不包含业务逻辑。

响应中不返回 credentials_encrypted 明文（仅返回 adapter_id / is_active / 时间戳）。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ExternalCredentialCreate(BaseModel):
    """创建/更新外部凭证请求。"""

    adapter_id: str = Field(..., description="适配器 ID: feishu/confluence/notion/obsidian")
    credentials: dict[str, Any] = Field(
        ...,
        description="平台凭证（明文，服务端加密存储）。"
        "飞书: {app_id, app_secret}; Confluence: {base_url, username, api_token} 或 {base_url, pat}; "
        "Notion: {integration_token}; Obsidian: {vault_path}",
    )
    is_active: bool = Field(True, description="是否启用")


class ExternalCredentialResponse(BaseModel):
    """外部凭证响应 — 不返回 credentials 明文。"""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    adapter_id: str
    is_active: bool
    created_at: datetime
    updated_at: datetime
    # 注意：不返回 credentials_encrypted（敏感数据）


class AdapterInfo(BaseModel):
    """适配器信息。"""

    adapter_id: str
    display_name: str
    supported_formats: list[str]


class CredentialTestResult(BaseModel):
    """凭证测试结果。"""

    adapter_id: str
    connected: bool
    message: str = ""
