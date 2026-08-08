"""样本文档灌库脚本（seed_documents.py）离线单元测试。

验证目标：
1. py_compile 通过 + 模块可 import（重依赖 sqlalchemy / app 模块延迟导入，
   无 DB 环境下 import 不报错）；
2. get_seed_documents() 纯函数：返回 30-50 篇、5 大场景齐全、密级分布合理、
   每篇字段齐全且内容非空（长度 >= 50）；
3. run_seed：mock AsyncSession，验证 insert 调用次数、幂等跳过逻辑、
   --clear 清理行为、--count 截断；
4. main：patch task_db_session 后端到端跑通，验证返回码与灌库结果。

mock 风格照 test_finetune_pipeline.py：AsyncMock / MagicMock。
"""

from __future__ import annotations

import importlib.util
import py_compile
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

FINETUNE_DIR = Path(__file__).parent.parent / "scripts" / "finetune"
SEED_PATH = FINETUNE_DIR / "seed_documents.py"


def _load_module():
    """按文件路径加载 seed_documents 模块（scripts/finetune 非 Python 包）。"""
    spec = importlib.util.spec_from_file_location("_finetune_seed_documents", SEED_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ------------------------------------------------------------------
# mock 工具：构造 session.execute 各类返回值
# ------------------------------------------------------------------


def _titles_result(titles: list[str]) -> MagicMock:
    """select(Document.title) 查询返回值：.scalars().all() -> [title, ...]。"""
    result = MagicMock()
    result.scalars.return_value.all.return_value = list(titles)
    return result


def _delete_result(rowcount: int) -> MagicMock:
    """delete 语句返回值：.rowcount。"""
    result = MagicMock()
    result.rowcount = rowcount
    return result


def _make_session(execute_side_effect: list) -> AsyncMock:
    """构造 AsyncSession mock：execute 按 side_effect 顺序返回，add/commit 可记录。"""
    session = AsyncMock()
    session.add = MagicMock()
    session.commit = AsyncMock()
    session.execute = AsyncMock(side_effect=execute_side_effect)
    return session


# ======================================================================
# 1. 语法与 import（重依赖延迟导入验证）
# ======================================================================


class TestCompileAndImport:
    def test_py_compile(self) -> None:
        py_compile.compile(str(SEED_PATH), doraise=True)

    def test_import_without_heavy_deps(self) -> None:
        """模块顶层仅标准库，import 不应加载 sqlalchemy / app 等重依赖。"""
        before = set(sys.modules)
        module = _load_module()
        assert module is not None
        assert callable(getattr(module, "get_seed_documents"))
        assert callable(getattr(module, "run_seed"))
        assert callable(getattr(module, "main"))
        # import 本模块不得新增加载 sqlalchemy / app（均延迟到函数内）
        newly_loaded = (set(sys.modules) - before) & {
            "sqlalchemy",
            "app",
            "app.database",
            "app.models",
            "app.models.knowledge",
        }
        assert not newly_loaded, f"顶层引入了重依赖: {newly_loaded}"


# ======================================================================
# 2. get_seed_documents 纯函数测试
# ======================================================================


class TestSeedDocuments:
    def test_count_in_range(self) -> None:
        docs = _load_module().get_seed_documents()
        assert 30 <= len(docs) <= 50

    def test_all_scenarios_present(self) -> None:
        mod = _load_module()
        docs = mod.get_seed_documents()
        scenarios = {d["category"] for d in docs}
        assert scenarios == set(mod.SCENARIOS)

    def test_field_completeness_and_content_length(self) -> None:
        docs = _load_module().get_seed_documents()
        for d in docs:
            assert set(d) == {"title", "content", "classification", "category", "source"}
            assert isinstance(d["title"], str) and d["title"].strip()
            assert isinstance(d["content"], str) and d["content"].strip()
            assert len(d["content"]) >= 50, f"内容过短: {d['title']}"
            assert d["classification"] in _load_module().VALID_CLASSIFICATIONS
            assert d["category"] in _load_module().SCENARIOS
            assert d["source"] == "seed"

    def test_titles_unique(self) -> None:
        docs = _load_module().get_seed_documents()
        titles = [d["title"] for d in docs]
        assert len(titles) == len(set(titles)), "存在重复标题，幂等逻辑会误跳过"

    def test_classification_distribution_reasonable(self) -> None:
        """密级分布：public ≈30% / internal ≈50% / confidential ≈20%。"""
        docs = _load_module().get_seed_documents()
        total = len(docs)
        counts = {"public": 0, "internal": 0, "confidential": 0}
        for d in docs:
            counts[d["classification"]] += 1
        # 三种密级都应存在（验证密级过滤覆盖）
        assert counts["public"] > 0
        assert counts["internal"] > 0
        assert counts["confidential"] > 0
        # 比例容差 ±10 个百分点
        assert abs(counts["public"] / total - 0.30) <= 0.10
        assert abs(counts["internal"] / total - 0.50) <= 0.10
        assert abs(counts["confidential"] / total - 0.20) <= 0.10

    def test_scenario_distribution(self) -> None:
        """5 大场景文档数量符合预期（IT/HR ~10、OA/产品 ~8、边界 ~4）。"""
        mod = _load_module()
        docs = mod.get_seed_documents()
        counts: dict[str, int] = {}
        for d in docs:
            counts[d["category"]] = counts.get(d["category"], 0) + 1
        assert counts[mod.SCENARIO_IT] == 10
        assert counts[mod.SCENARIO_HR] == 10
        assert counts[mod.SCENARIO_OA] == 8
        assert counts[mod.SCENARIO_PRODUCT] == 8
        assert counts[mod.SCENARIO_BOUNDARY] == 4

    def test_content_realistic_not_placeholder(self) -> None:
        """内容应为企业真实文档，非 lorem ipsum 占位符。"""
        docs = _load_module().get_seed_documents()
        for d in docs:
            assert "lorem" not in d["content"].lower()
            assert "ipsum" not in d["content"].lower()
            # 每篇至少 200 字（贴近真实文档信息量）
            assert len(d["content"]) >= 200, f"内容信息量不足: {d['title']}"


# ======================================================================
# 3. run_seed mock DB 测试
# ======================================================================


class TestRunSeed:
    """run_seed 核心逻辑测试（mock AsyncSession，显式传 kb_id/owner_id 跳过自动解析）。"""

    @pytest.mark.asyncio
    async def test_insert_all_when_empty(self) -> None:
        """租户无已有文档：全部新增，add 调用次数 == 文档总数。"""
        mod = _load_module()
        total = len(mod.get_seed_documents())
        session = _make_session([_titles_result([])])  # 仅 1 次 titles 查询

        result = await mod.run_seed(
            session,
            tenant_id=uuid4(),
            kb_id=uuid4(),
            owner_id=uuid4(),
        )

        assert result["inserted"] == total
        assert result["skipped"] == 0
        assert result["cleared"] == 0
        assert session.add.call_count == total
        assert session.execute.await_count == 1  # 仅 titles 查询
        session.commit.assert_awaited_once()
        # 场景与密级分布统计与源数据一致
        assert sum(result["scenario_dist"].values()) == total
        assert sum(result["classification_dist"].values()) == total

    @pytest.mark.asyncio
    async def test_idempotent_skip_existing(self) -> None:
        """相同 title 已存在则跳过：inserted 减少、skipped 累加。"""
        mod = _load_module()
        docs = mod.get_seed_documents()
        existing = [d["title"] for d in docs[:7]]  # 前 7 篇已存在
        session = _make_session([_titles_result(existing)])

        result = await mod.run_seed(
            session,
            tenant_id=uuid4(),
            kb_id=uuid4(),
            owner_id=uuid4(),
        )

        assert result["inserted"] == len(docs) - 7
        assert result["skipped"] == 7
        assert session.add.call_count == len(docs) - 7
        # 跳过的标题不应被 add
        added_titles = {call.args[0].title for call in session.add.call_args_list}
        assert set(existing).isdisjoint(added_titles)

    @pytest.mark.asyncio
    async def test_clear_deletes_seed_docs_first(self) -> None:
        """--clear 先执行 delete（rowcount=12），再查 titles，最后全量新增。"""
        mod = _load_module()
        total = len(mod.get_seed_documents())
        session = _make_session(
            [
                _delete_result(12),  # 1. delete 返回 rowcount
                _titles_result([]),  # 2. 清理后 titles 为空
            ]
        )

        result = await mod.run_seed(
            session,
            tenant_id=uuid4(),
            clear=True,
            kb_id=uuid4(),
            owner_id=uuid4(),
        )

        assert result["cleared"] == 12
        assert result["inserted"] == total
        assert result["skipped"] == 0
        assert session.execute.await_count == 2
        # 两次 commit：clear 后一次 + 灌库后一次
        assert session.commit.await_count == 2

    @pytest.mark.asyncio
    async def test_count_truncates(self) -> None:
        """--count N 仅灌入前 N 篇。"""
        mod = _load_module()
        session = _make_session([_titles_result([])])

        result = await mod.run_seed(
            session,
            tenant_id=uuid4(),
            count=5,
            kb_id=uuid4(),
            owner_id=uuid4(),
        )

        assert result["inserted"] == 5
        assert session.add.call_count == 5
        # 截断的 5 篇场景分布与内置前 5 篇一致
        first5 = mod.get_seed_documents()[:5]
        expected_scenario = {}
        for d in first5:
            expected_scenario[d["category"]] = expected_scenario.get(d["category"], 0) + 1
        assert result["scenario_dist"] == expected_scenario

    @pytest.mark.asyncio
    async def test_inserted_document_fields(self) -> None:
        """新增的 Document 对象字段齐全且与源数据一致。"""
        mod = _load_module()
        session = _make_session([_titles_result([])])
        tid = uuid4()
        kb_id = uuid4()
        owner_id = uuid4()

        await mod.run_seed(
            session, tenant_id=tid, kb_id=kb_id, owner_id=owner_id
        )

        first = session.add.call_args_list[0].args[0]
        assert first.kb_id == kb_id
        assert first.owner_id == owner_id
        assert first.tenant_id == tid
        assert first.status == "published"
        assert first.doc_type == "md"
        # Document 无 source 列，来源标记写入 content_json
        assert not hasattr(first, "source")
        assert first.content_json["source"] == "seed"
        assert first.content_json["scenario"] == first.category
        assert first.char_count == len(first.content_text)
        assert first.classification in mod.VALID_CLASSIFICATIONS

    @pytest.mark.asyncio
    async def test_clear_then_skip_combo(self) -> None:
        """--clear 后仍存在非 seed 文档（titles 非空）→ 跳过对应标题。"""
        mod = _load_module()
        docs = mod.get_seed_documents()
        # 清理后仍有 3 篇非 seed 文档标题与内置文档重名 → 跳过
        leftover = [d["title"] for d in docs[:3]]
        session = _make_session(
            [_delete_result(5), _titles_result(leftover)]
        )

        result = await mod.run_seed(
            session,
            tenant_id=uuid4(),
            clear=True,
            kb_id=uuid4(),
            owner_id=uuid4(),
        )

        assert result["cleared"] == 5
        assert result["skipped"] == 3
        assert result["inserted"] == len(docs) - 3


# ======================================================================
# 4. main 端到端（patch task_db_session）
# ======================================================================


class TestMain:
    def test_main_insert_all(self) -> None:
        """main 端到端：patch task_db_session，验证返回码 0 与灌库数。"""
        mod = _load_module()
        total = len(mod.get_seed_documents())
        session = _make_session([_titles_result([])])

        @asynccontextmanager
        async def fake_session():
            yield session

        with patch("app.database.task_db_session", fake_session):
            rc = mod.main(
                [
                    "--tenant_id",
                    str(uuid4()),
                    "--kb_id",
                    str(uuid4()),
                    "--owner_id",
                    str(uuid4()),
                ]
            )

        assert rc == 0
        assert session.add.call_count == total

    def test_main_with_clear(self) -> None:
        """main --clear：先 delete 再灌库。"""
        mod = _load_module()
        total = len(mod.get_seed_documents())
        session = _make_session(
            [_delete_result(8), _titles_result([])]
        )

        @asynccontextmanager
        async def fake_session():
            yield session

        with patch("app.database.task_db_session", fake_session):
            rc = mod.main(
                [
                    "--tenant_id",
                    str(uuid4()),
                    "--clear",
                    "--kb_id",
                    str(uuid4()),
                    "--owner_id",
                    str(uuid4()),
                ]
            )

        assert rc == 0
        assert session.execute.await_count == 2
        assert session.add.call_count == total

    def test_main_requires_tenant_id(self) -> None:
        """--tenant_id 必填，缺失应报错退出（SystemExit）。"""
        mod = _load_module()
        with pytest.raises(SystemExit):
            mod.main(["--kb_id", str(uuid4())])
