"""
文档来源适配器注册表 — 管理适配器的注册、发现和获取。

遵循开闭原则：新增平台适配器只需注册到工厂，无需修改调用方代码。

使用方式::

    from app.document.source_adapters.registry import adapter_registry

    # 获取适配器
    adapter = adapter_registry.get("confluence")
    if adapter:
        doc = await adapter.fetch(page_id, credentials)

    # 注册自定义适配器
    adapter_registry.register(MyAdapter())
"""
from __future__ import annotations

from app.document.source_adapters.base import DocumentSourceAdapter
from app.utils.logger import get_logger

log = get_logger(__name__)


class SourceAdapterRegistry:
    """文档来源适配器注册表 — 管理 CRUD 和获取。

    与 connectors/registry.py 的设计一致，但面向文档来源平台。
    """

    def __init__(self) -> None:
        self._adapters: dict[str, DocumentSourceAdapter] = {}

    def register(self, adapter: DocumentSourceAdapter) -> None:
        """注册适配器。

        Args:
            adapter: 适配器实例。
        """
        self._adapters[adapter.adapter_id] = adapter
        log.info(
            "source_adapter.registered",
            adapter_id=adapter.adapter_id,
            display_name=adapter.display_name,
        )

    def unregister(self, adapter_id: str) -> bool:
        """注销适配器。

        Args:
            adapter_id: 适配器 ID。

        Returns:
            是否成功注销。
        """
        if adapter_id in self._adapters:
            del self._adapters[adapter_id]
            log.info("source_adapter.unregistered", adapter_id=adapter_id)
            return True
        return False

    def get(self, adapter_id: str) -> DocumentSourceAdapter | None:
        """获取指定适配器。

        Args:
            adapter_id: 适配器 ID（confluence / obsidian / feishu / notion）。

        Returns:
            适配器实例或 None。
        """
        return self._adapters.get(adapter_id)

    def get_all(self) -> list[DocumentSourceAdapter]:
        """获取所有已注册适配器。"""
        return list(self._adapters.values())

    def list_adapters(self) -> list[dict[str, Any]]:
        """列出所有适配器及其信息。"""
        return [
            {
                "adapter_id": a.adapter_id,
                "display_name": a.display_name,
                "supported_formats": list(a.supported_formats),
            }
            for a in self._adapters.values()
        ]


# 避免在类型注解中引用 Any 时未导入
from typing import Any  # noqa: E402


# ------------------------------------------------------------------
# 全局单例 — 延迟注册内置适配器
# ------------------------------------------------------------------

adapter_registry = SourceAdapterRegistry()


def _register_builtin_adapters() -> None:
    """注册内置适配器 — 延迟导入避免循环依赖。"""
    # P0 适配器
    try:
        from app.document.source_adapters.confluence_adapter import ConfluenceAdapter

        adapter_registry.register(ConfluenceAdapter())
    except ImportError as e:
        log.debug("source_adapter.confluence_skip", error=str(e))

    try:
        from app.document.source_adapters.obsidian_adapter import ObsidianAdapter

        adapter_registry.register(ObsidianAdapter())
    except ImportError as e:
        log.debug("source_adapter.obsidian_skip", error=str(e))

    # P1 适配器
    try:
        from app.document.source_adapters.feishu_adapter import FeishuAdapter

        adapter_registry.register(FeishuAdapter())
    except ImportError as e:
        log.debug("source_adapter.feishu_skip", error=str(e))

    try:
        from app.document.source_adapters.notion_adapter import NotionAdapter

        adapter_registry.register(NotionAdapter())
    except ImportError as e:
        log.debug("source_adapter.notion_skip", error=str(e))


# 模块加载时自动注册
_register_builtin_adapters()
