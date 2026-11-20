#!/usr/bin/env python3
"""为 RLS 门禁准备最小测试数据 — 两个租户 × 七张关键表各一行。

## 为什么需要这个脚本

``verify_rls_isolation.py`` 用真实数据验证隔离：找出数据量最大的两个租户，
对每个关键表比较「无 GUC 可见 0 行 / 租户 A 只见 A 行 / 跨租户 '*' 可见全部」。
CI 用全新空库跑迁移后没有任何数据，脚本会因「少于 2 个有数据的租户」返回 2，
门禁直接红——不是策略有问题，是没有可比的数据。

本脚本在 owner 连接下（设 app.tenant_id='*' 跨租户写入）灌入最小数据集：
两个租户，每个租户在 7 张 critical 表各一行，满足 verify 的对比前提。

## 用法

    python scripts/seed_rls_gate_data.py --url "postgresql://owner:***@host:5432/db"

退出码：0 = 数据就绪；2 = 连接/写入失败。

幂等：先按固定 UUID 删除再插入，重复执行不产生重复数据。
仅用于 CI / 测试库，禁止在生产执行（会插入固定 ID 的假数据）。
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
import uuid

try:
    import asyncpg
except ImportError:  # pragma: no cover
    asyncpg = None  # type: ignore[assignment]

# 固定租户 UUID — 与 verify_rls_isolation.py 的探针约定一致，便于复现
TENANT_A = "11111111-1111-1111-1111-111111111111"
TENANT_B = "22222222-2222-2222-2222-222222222222"

# 每张表的固定行 UUID（按 租户|表 生成，保证幂等 + 跨租户唯一）
_NAMESPACE = uuid.UUID("3f2a1c88-9d4e-4b17-a0c3-5e6f7d8b9a20")


def _uid(tenant: str, table: str) -> str:
    return str(uuid.uuid5(_NAMESPACE, f"{tenant}|{table}"))


def _to_asyncpg_dsn(url: str) -> str:
    return url.replace("postgresql+asyncpg://", "postgresql://", 1)


async def seed(url: str) -> int:
    if asyncpg is None:
        print("需要 asyncpg", file=sys.stderr)
        return 2

    conn = await asyncpg.connect(_to_asyncpg_dsn(url))
    try:
        # 跨租户写入（owner + '*' GUC），否则 FORCE RLS 下看不到别租户的行
        await conn.execute("SELECT set_config('app.tenant_id', '*', true)")
        async with conn.transaction():
            for tid in (TENANT_A, TENANT_B):
                user_id = _uid(tid, "users")
                kb_id = _uid(tid, "knowledge_bases")
                doc_id = _uid(tid, "documents")
                question_id = _uid(tid, "qa_questions")

                # tenants（plan/max_users/max_storage 无默认，必须显式给）
                await conn.execute(
                    """INSERT INTO public.tenants (id, name, plan, max_users, max_storage)
                       VALUES ($1, $2, 'pro', 100, 1073741824)
                       ON CONFLICT (id) DO NOTHING""",
                    uuid.UUID(tid),
                    f"rls-gate-{tid[:8]}",
                )
                # users
                await conn.execute(
                    """INSERT INTO public.users
                       (id, tenant_id, email, hashed_password, name, role, clearance_level, is_active)
                       VALUES ($1, $2, $3, 'gate-placeholder', $4, 'admin', 'internal', true)
                       ON CONFLICT (id) DO NOTHING""",
                    uuid.UUID(user_id),
                    uuid.UUID(tid),
                    f"gate-{tid[:8]}@local.test",
                    f"gate-user-{tid[:8]}",
                )
                # knowledge_bases
                await conn.execute(
                    """INSERT INTO public.knowledge_bases (id, tenant_id, name, visibility, owner_id)
                       VALUES ($1, $2, $3, 'private', $4)
                       ON CONFLICT (id) DO NOTHING""",
                    uuid.UUID(kb_id),
                    uuid.UUID(tid),
                    f"gate-kb-{tid[:8]}",
                    uuid.UUID(user_id),
                )
                # documents
                await conn.execute(
                    """INSERT INTO public.documents
                       (id, tenant_id, kb_id, title, doc_type, status, owner_id, classification, view_count)
                       VALUES ($1, $2, $3, $4, 'md', 'published', $5, 'internal', 0)
                       ON CONFLICT (id) DO NOTHING""",
                    uuid.UUID(doc_id),
                    uuid.UUID(tid),
                    uuid.UUID(kb_id),
                    f"gate-doc-{tid[:8]}",
                    uuid.UUID(user_id),
                )
                # document_chunks
                await conn.execute(
                    """INSERT INTO public.document_chunks (id, tenant_id, doc_id, kb_id, content_text)
                       VALUES ($1, $2, $3, $4, 'rls gate probe chunk')
                       ON CONFLICT (id) DO NOTHING""",
                    uuid.UUID(_uid(tid, "document_chunks")),
                    uuid.UUID(tid),
                    uuid.UUID(doc_id),
                    uuid.UUID(kb_id),
                )
                # tickets
                ticket_id = _uid(tid, "tickets")
                await conn.execute(
                    """INSERT INTO public.tickets (id, tenant_id, user_id, title, description)
                       VALUES ($1, $2, $3, $4, 'rls gate probe ticket')
                       ON CONFLICT (id) DO NOTHING""",
                    uuid.UUID(ticket_id),
                    uuid.UUID(tid),
                    uuid.UUID(user_id),
                    f"gate-ticket-{tid[:8]}",
                )
                # qa_questions
                await conn.execute(
                    """INSERT INTO public.qa_questions
                       (id, tenant_id, user_id, title, content, status, view_count, answer_count)
                       VALUES ($1, $2, $3, $4, 'rls gate probe question', 'published', 0, 1)
                       ON CONFLICT (id) DO NOTHING""",
                    uuid.UUID(question_id),
                    uuid.UUID(tid),
                    uuid.UUID(user_id),
                    f"gate-question-{tid[:8]}",
                )
                # qa_answers
                await conn.execute(
                    """INSERT INTO public.qa_answers
                       (id, tenant_id, question_id, user_id, content, is_accepted, is_ai_generated, vote_count)
                       VALUES ($1, $2, $3, $4, 'rls gate probe answer', false, false, 0)
                       ON CONFLICT (id) DO NOTHING""",
                    uuid.UUID(_uid(tid, "qa_answers")),
                    uuid.UUID(tid),
                    uuid.UUID(question_id),
                    uuid.UUID(user_id),
                )

        print(f"✓ RLS 门禁数据就绪：租户 {TENANT_A} / {TENANT_B} × 7 张关键表")
        return 0
    except Exception as exc:  # noqa: BLE001
        print(f"✗ 写入失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    finally:
        await conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="为 RLS 门禁灌入两租户测试数据")
    parser.add_argument("--url", default=os.environ.get("DATABASE_URL"), help="owner 连接串")
    args = parser.parse_args()
    if not args.url:
        print("ERROR: 未提供 --url 或 $DATABASE_URL", file=sys.stderr)
        return 2
    return asyncio.run(seed(args.url))


if __name__ == "__main__":
    raise SystemExit(main())
