"""B2 验收闸门 — 记忆向量列宽必须等于当前部署模式 embedder 的输出维度。

四组：
1. ``_cosine_similarity`` 维度不一致必须显式报错（zip 截断会静默算错相似度）；
2. JSONB 降级路径遇到维度不齐的存量向量必须报错，不得截断后继续排序；
3. 跨部署模式（saas/saas_dashscope/private_*）：生产 ORM 在**真实 PG** 里
   建出的列宽 == 生产 embedder 注册表产出的 dim。模型在导入期捕获配置，
   主测试进程的环境是 saas_dashscope，所以每个模式用独立子进程建表 ——
   既不 reload 污染主进程 mapper，又打到真实 DDL 边界；
4. 生产对齐迁移：把错宽列重建为当前模式 embedder.dim（存量库修复路径）。

红基线（修复前）：组 1/2 静默截断不报错；组 3 saas 模式列宽 1024 ≠ 3072。
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]
MODES = ["saas", "saas_dashscope", "private_overseas", "private_domestic"]


def _embedder_dim_for_mode(mode: str, monkeypatch) -> int:
    """生产注册表侧的 dim —— 与列宽侧不同源（一个来自工厂实例，一个来自真实 PG）。

    环境 shim：本机 NO_PROXY 含裸 ``::1``，httpx 解析 no_proxy 时抛
    InvalidURL(':1]')（即 task.md 勘误里 52 条既有失败的共性根因，范围外）。
    这里只清理测试进程的环境变量，不改产品代码。
    """
    import app.llm.embedder as emb
    from app.config import Settings

    monkeypatch.setenv("NO_PROXY", "localhost,127.0.0.1")
    monkeypatch.setenv("no_proxy", "localhost,127.0.0.1")
    s = Settings(
        DEPLOY_MODE=mode,
        OPENAI_API_KEY="pytest-dummy-key",
        DASHSCOPE_API_KEY="pytest-dummy-key",
    )
    monkeypatch.setattr(emb, "settings", s)
    emb.get_embedder.cache_clear()
    try:
        return emb.get_embedder().dim
    finally:
        emb.get_embedder.cache_clear()


class TestB2CosineDimension:
    def test_mismatched_dims_raise(self):
        from app.memory.mem0_manager import _cosine_similarity

        with pytest.raises(ValueError):
            _cosine_similarity([1.0, 0.0, 0.0], [1.0, 0.0])

    def test_equal_dims_still_compute(self):
        """正向证人：维度一致时计算不受影响。"""
        from app.memory.mem0_manager import _cosine_similarity

        assert _cosine_similarity([1.0, 0.0], [1.0, 0.0]) == pytest.approx(1.0)
        assert _cosine_similarity([], [1.0]) == 0.0


class TestB2JsonbFallback:
    async def test_mismatched_stored_embedding_raises(self, db_session):
        """存量向量与 query 维度不齐：降级路径必须显式报错，不得截断排序。"""
        import uuid

        from app.memory.mem0_manager import Mem0Manager
        from app.models.memory import MemoryFact

        user_id = uuid.uuid4()
        db_session.add(
            MemoryFact(
                user_id=user_id,
                category="fact",
                fact_text="旧模型产出的三维向量",
                embedding=[1.0, 1.0, 1.0],
                embedding_vec=None,
            )
        )
        await db_session.flush()

        mgr = Mem0Manager(db_session)

        class _Q:
            dim = 4

            async def embed(self, texts):
                return [[1.0] * 4 for _ in texts]

        mgr._embedder = _Q()
        with pytest.raises(ValueError):
            await mgr.search_facts(user_id=user_id, query="任意查询")

    async def test_matching_stored_embedding_returns_fact(self, db_session):
        """正向证人：维度一致时 JSONB 降级正常召回。"""
        import uuid

        from app.memory.mem0_manager import Mem0Manager
        from app.models.memory import MemoryFact

        user_id = uuid.uuid4()
        db_session.add(
            MemoryFact(
                user_id=user_id,
                category="fact",
                fact_text="维度一致的向量",
                embedding=[1.0, 1.0, 1.0],
                embedding_vec=None,
            )
        )
        await db_session.flush()

        mgr = Mem0Manager(db_session)

        class _Q:
            dim = 3

            async def embed(self, texts):
                return [[1.0] * 3 for _ in texts]

        mgr._embedder = _Q()
        results = await mgr.search_facts(
            user_id=user_id, query="任意查询", update_access_stats=False
        )
        assert [f.fact_text for f in results] == ["维度一致的向量"]


_SUBPROCESS_PROBE = """
import asyncio, os
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from app.models.memory import MemoryFact

async def main():
    eng = create_async_engine(os.environ["DATABASE_URL"])
    async with eng.begin() as conn:
        await conn.execute(text("DROP TABLE IF EXISTS memory_facts CASCADE"))
        await conn.run_sync(lambda sc: MemoryFact.__table__.create(sc))
        width = (await conn.execute(text(
            "select format_type(a.atttypid, a.atttypmod) "
            "from pg_attribute a join pg_class c on c.oid = a.attrelid "
            "where c.relname = 'memory_facts' and a.attname = 'embedding_vec'"
        ))).scalar()
        print(width)
    await eng.dispose()

asyncio.run(main())
"""


class TestB2ModelColumnWidth:
    @pytest.mark.parametrize("mode", MODES)
    def test_real_pg_column_width_equals_embedder_dim(self, mode, monkeypatch):
        expected = _embedder_dim_for_mode(mode, monkeypatch)
        env = dict(os.environ, DEPLOY_MODE=mode)
        proc = subprocess.run(
            [sys.executable, "-c", _SUBPROCESS_PROBE],
            cwd=str(BACKEND_DIR),
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert proc.returncode == 0, f"子进程建表失败：{proc.stderr[-2000:]}"
        width = proc.stdout.strip().splitlines()[-1]
        assert (
            width == f"vector({expected})"
        ), f"DEPLOY_MODE={mode}：真实列宽 {width} ≠ embedder.dim {expected}"


def _load_migration(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestB2AlignMigration:
    """生产对齐迁移：错宽列 → 当前模式 embedder.dim（真实 PG 执行 upgrade）。"""

    @pytest.fixture
    def migration(self):
        hits = sorted(
            (BACKEND_DIR / "alembic" / "versions").glob(
                "*align_memory_vec_dim_deploy_mode*.py"
            )
        )
        assert hits, "缺少 B2 对齐迁移（*align_memory_vec_dim_deploy_mode*.py）"
        return _load_migration("b2_align_migration", hits[-1])

    @pytest.mark.parametrize("mode", MODES)
    async def test_upgrade_realigns_width_to_embedder(
        self, mode, migration, monkeypatch
    ):
        import sqlalchemy as sa
        from sqlalchemy.ext.asyncio import create_async_engine

        from alembic.migration import MigrationContext
        from alembic.operations import Operations
        from app.config import Settings

        expected = _embedder_dim_for_mode(mode, monkeypatch)
        s = Settings(
            DEPLOY_MODE=mode,
            OPENAI_API_KEY="pytest-dummy-key",
            DASHSCOPE_API_KEY="pytest-dummy-key",
        )
        monkeypatch.setattr(migration, "get_settings", lambda: s)

        engine = create_async_engine(os.environ["DATABASE_URL"])
        try:
            async with engine.begin() as conn:
                await conn.execute(sa.text("DROP TABLE IF EXISTS memory_facts CASCADE"))
                await conn.execute(
                    sa.text(
                        "CREATE TABLE memory_facts ("
                        "id uuid primary key, embedding jsonb, embedding_vec vector(7))"
                    )
                )

            def _run_upgrade(sync_conn):
                ctx = MigrationContext.configure(sync_conn)
                ops = Operations(ctx)
                ops._install_proxy()
                try:
                    migration.upgrade()
                finally:
                    ops._remove_proxy()

            async with engine.begin() as conn:
                await conn.run_sync(_run_upgrade)
                width = (
                    await conn.execute(
                        sa.text(
                            "select format_type(a.atttypid, a.atttypmod) "
                            "from pg_attribute a join pg_class c on c.oid = a.attrelid "
                            "where c.relname = 'memory_facts' "
                            "and a.attname = 'embedding_vec'"
                        )
                    )
                ).scalar()
                index_name = (
                    await conn.execute(
                        sa.text(
                            "select indexname from pg_indexes "
                            "where tablename = 'memory_facts' "
                            "and indexname = 'ix_memory_facts_embedding_vec'"
                        )
                    )
                ).scalar()
            assert width == f"vector({expected})"
            # pgvector ivfflat 上限 2000 维：超宽列不建索引（精确扫描），
            # 其余模式保持原有索引
            if expected > 2000:
                assert index_name is None
            else:
                assert index_name == "ix_memory_facts_embedding_vec"
        finally:
            async with engine.begin() as conn:
                await conn.execute(sa.text("DROP TABLE IF EXISTS memory_facts CASCADE"))
            await engine.dispose()

    def test_private_finetuned_column_fallback_declared(self, migration, monkeypatch):
        """private_finetuned 已注册 TEI embedder（B10）：列宽 1024 与输出维度一致。"""
        from app.config import Settings
        from app.llm.embedder import TEIEmbedder, memory_embedding_dim

        s = Settings(DEPLOY_MODE="private_finetuned")
        assert memory_embedding_dim(s) == TEIEmbedder.dim
