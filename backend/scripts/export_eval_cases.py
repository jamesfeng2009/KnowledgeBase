#!/usr/bin/env python
"""从 v_delivery_chain 视图导出 badcase 链路为评测语料（P2b 评测集回流）。

筛选口径（默认）：
    is_badcase = true（含 complaint / bug 反馈的回答）

输出 JSONL（一行一个用例）字段：
    case_id        assistant 消息 ID（唯一锚点，便于回溯归因）
    question       同会话紧邻的 user 提问（Intent）
    answer_excerpt 回答摘要 ≤500 字符（Output）
    citations      引用卡片列表
    model_used / answer_tokens
    tool_calls / tool_errors / tool_names / has_error（Process 概览）
    feedback_types 反馈类型列表
    is_badcase
    conversation_id / answered_at（回溯定位）

用法::

    cd backend && .venv/bin/python scripts/export_eval_cases.py \
        [--days 30] [--limit 500] [--out eval_cases/badcases.jsonl] [--all]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

_BACKEND_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)

from sqlalchemy import text  # noqa: E402

from app.database import async_session_factory  # noqa: E402
from app.utils.logger import get_logger  # noqa: E402

logger = get_logger(__name__)


def build_eval_case(row) -> dict:
    """视图行 → 评测用例 dict（JSON 可序列化）。"""
    return {
        "case_id": str(row.message_id),
        "question": row.user_question or "",
        "answer_excerpt": row.answer_excerpt or "",
        "citations": list(row.citations or []),
        "model_used": row.model_used,
        "answer_tokens": int(row.answer_tokens or 0),
        "tool_calls": int(row.tool_calls or 0),
        "tool_errors": int(row.tool_errors or 0),
        "tool_names": list(row.tool_names or []),
        "has_error": bool(row.has_error),
        "feedback_types": list(row.feedback_types or []),
        "is_badcase": bool(row.is_badcase),
        "conversation_id": str(row.conversation_id),
        "answered_at": row.answered_at.isoformat() if row.answered_at else None,
    }


async def export_cases(
    days: int,
    limit: int,
    out_path: Path,
    only_badcase: bool = True,
) -> int:
    """查询视图并写出 JSONL，返回导出条数。"""
    since = datetime.now(timezone.utc) - timedelta(days=days)
    where = [
        "(:tid::uuid IS NULL OR tenant_id = :tid::uuid)",
        "answered_at >= :since",
    ]
    if only_badcase:
        where.append("is_badcase = true")
    sql = text(
        f"SELECT * FROM v_delivery_chain WHERE {' AND '.join(where)} "
        "ORDER BY answered_at DESC LIMIT :limit"
    )

    async with async_session_factory() as db:
        rows = (
            await db.execute(
                sql,
                {"tid": None, "since": since, "limit": limit},
            )
        ).fetchall()

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(
                json.dumps(build_eval_case(row), ensure_ascii=False) + "\n"
            )

    logger.info(
        "export_eval_cases.done", extra={"count": len(rows), "out": str(out_path)}
    )
    return len(rows)


def main() -> int:
    parser = argparse.ArgumentParser(description="导出交付链 badcase 评测语料")
    parser.add_argument(
        "--days", type=int, default=30, help="回溯天数（默认 30）"
    )
    parser.add_argument(
        "--limit", type=int, default=500, help="最多导出条数（默认 500）"
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=Path("eval_cases") / f"delivery_badcases_{datetime.now(timezone.utc).strftime('%Y%m%d')}.jsonl",
        help="输出 JSONL 路径",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="导出全部链路（默认仅 badcase）",
    )
    args = parser.parse_args()

    count = asyncio.run(
        export_cases(args.days, args.limit, args.out, only_badcase=not args.all)
    )
    print(f"导出完成：{count} 条 → {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
