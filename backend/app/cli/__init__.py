"""CLI 客户端包 — 供 scripts/ekb_cli.py 与第三方脚本复用。"""

from app.cli.client import EkbClient, EkbClientError

__all__ = ["EkbClient", "EkbClientError"]
