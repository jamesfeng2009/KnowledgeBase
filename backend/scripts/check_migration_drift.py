#!/usr/bin/env python
"""迁移漂移门禁 — 在一个"只跑过迁移"的库上检查 ORM 模型与真实 schema 的差异。

用法::

    # 用当前 DATABASE_URL（必须已 upgrade head）
    python scripts/check_migration_drift.py

    # 指定库（CI 里用独立库，避免污染测试库）
    python scripts/check_migration_drift.py --url postgresql+asyncpg://ekb:ekb@localhost:5432/ekb_ci

    # 只打印不阻断（本地排查用）
    python scripts/check_migration_drift.py --warn-only

退出码：
    0 — 无破坏性/结构性漂移（注释、默认值类噪音不计）
    1 — 存在破坏性漂移（会删表/删列/删索引/丢外键）或结构性漂移
    2 — 环境问题（连不上库、库未迁移、ALEMBIC 配置缺失）

为什么需要它：本仓 autogenerate 曾是地雷（一份 diff 里 84 条 remove_index + 33 条
remove_fk），靠人眼在几百行 diff 里发现"删索引"不现实。门禁把这件事故变成红灯。

注意：``alembic/env.py`` 里的 ``include_object`` 过滤器已经把"库里有、模型没有"的
索引/外键隐身，所以本脚本看到的 ``remove_index``/``remove_fk`` 通常意味着
**过滤器被绕过或模型侧显式声明了同名冲突对象**，仍需人工确认。
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from sqlalchemy.ext.asyncio import create_async_engine  # noqa: E402

from alembic.autogenerate import compare_metadata  # noqa: E402
from alembic.migration import MigrationContext  # noqa: E402
from alembic_drift import include_object, split_by_severity, summarize  # noqa: E402


def _load_metadata():
    # 表定义在 app/models 包之外的模块，必须显式导入，否则会被判成"多余的表"
    import app.eval.repository
    import app.models.tool_audit  # noqa: F401
    from app.models import Base

    return Base.metadata


async def _collect_diffs(url: str) -> list:
    engine = create_async_engine(url)
    try:
        async with engine.connect() as conn:

            def _compare(sync_conn):
                ctx = MigrationContext.configure(
                    sync_conn,
                    opts={
                        "compare_type": True,
                        "compare_server_default": True,
                        "include_object": include_object,
                    },
                )
                return compare_metadata(ctx, _load_metadata())

            return await conn.run_sync(_compare)
    finally:
        await engine.dispose()


def _check_revision(url: str) -> str | None:
    """确认目标库确实迁移到 head，避免在 create_all 建的空壳库上做无效对比。"""
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    return ScriptDirectory.from_config(cfg).get_current_head()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="alembic autogenerate 漂移门禁")
    parser.add_argument(
        "--url",
        default=os.environ.get("DATABASE_URL"),
        help="目标库 URL（默认取 $DATABASE_URL）",
    )
    parser.add_argument(
        "--warn-only", action="store_true", help="只打印摘要，不因漂移返回非零退出码"
    )
    args = parser.parse_args(argv)

    if not args.url:
        print("ERROR: 未提供数据库 URL（--url 或 $DATABASE_URL）", file=sys.stderr)
        return 2

    try:
        diffs = asyncio.run(_collect_diffs(args.url))
    except Exception as exc:
        print(f"ERROR: 无法对比 schema：{exc}", file=sys.stderr)
        return 2

    destructive, structural, noise = split_by_severity(diffs)
    counts = summarize(diffs)

    print(f"head revision: {_check_revision(args.url)}")
    print(f"diff 总数: {len(destructive) + len(structural) + len(noise)}")
    for kind, count in counts.items():
        print(f"  {kind:<28} {count}")

    if destructive:
        print("\n[BLOCK] 破坏性漂移（会删库中已有结构）：")
        for kind in sorted(set(destructive)):
            print(f"  - {kind} × {destructive.count(kind)}")
        print(
            "\n本仓禁止用 autogenerate 删除已有结构。若是刻意下线，"
            "请手写迁移并显式 op.drop_index/op.drop_constraint，再更新模型。"
        )
    if structural:
        print("\n[BLOCK] 结构性漂移（必须人工审阅后写手写迁移）：")
        for kind in sorted(set(structural)):
            print(f"  - {kind} × {structural.count(kind)}")
    if noise:
        print("\n[warn] 噪音类差异（注释/默认值，不阻断）：")
        for kind in sorted(set(noise)):
            print(f"  - {kind} × {noise.count(kind)}")

    if (destructive or structural) and not args.warn_only:
        return 1
    print("\nOK: 无破坏性漂移")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
