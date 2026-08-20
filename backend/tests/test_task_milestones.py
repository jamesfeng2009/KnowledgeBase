"""
P2-13 长任务里程碑 checkpoint + 超时分级测试。

覆盖范围：
    - append_milestone_to_state 纯函数（序号/时间戳/原地追加）
    - CheckpointManager 里程碑方法（save/get/completed/latest，Fake DB）
    - run_stages_with_milestones：全量执行、断点恢复跳过、单步超时记录并重抛
"""
from __future__ import annotations

import asyncio
import sys
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.memory.checkpoint import (
    MILESTONES_FIELD,
    CheckpointManager,
    append_milestone_to_state,
)
from tasks.milestone_runner import (
    MilestoneStage,
    run_stages_with_milestones,
)


@pytest.fixture(autouse=True)
def _mock_celery(monkeypatch):
    """测试内隔离 celery，避免模块级 mock 污染 sys.modules 影响其它测试。"""
    mock_celery = MagicMock()
    mock_celery.Celery = MagicMock
    monkeypatch.setitem(sys.modules, "celery", mock_celery)
    mock_celery_app = MagicMock()
    mock_celery_app.celery_app = MagicMock()
    monkeypatch.setitem(sys.modules, "celery_app", mock_celery_app)


# ======================================================================
# append_milestone_to_state 纯函数测试
# ======================================================================


class TestAppendMilestoneToState:
    """里程碑追加纯函数测试。"""

    def test_appends_with_seq_and_timestamp(self) -> None:
        """追加里程碑带序号与时间戳。"""
        state: dict = {}
        milestones = append_milestone_to_state(state, "parse", {"status": "done"})
        assert len(milestones) == 1
        entry = milestones[0]
        assert entry["seq"] == 1
        assert entry["name"] == "parse"
        assert entry["detail"] == {"status": "done"}
        assert entry["timestamp"]
        # 原地修改 state
        assert state[MILESTONES_FIELD] is milestones

    def test_seq_increments(self) -> None:
        """序号随追加递增。"""
        state: dict = {}
        append_milestone_to_state(state, "parse")
        append_milestone_to_state(state, "index")
        append_milestone_to_state(state, "graph")
        seqs = [m["seq"] for m in state[MILESTONES_FIELD]]
        assert seqs == [1, 2, 3]

    def test_default_detail_is_empty_dict(self) -> None:
        """detail 缺省为空 dict。"""
        state: dict = {}
        append_milestone_to_state(state, "parse")
        assert state[MILESTONES_FIELD][0]["detail"] == {}


# ======================================================================
# CheckpointManager 里程碑方法测试（Fake DB）
# ======================================================================


class _FakeResult:
    def __init__(self, row) -> None:
        self._row = row

    def first(self):
        return self._row


class TestCheckpointManagerMilestones:
    """CheckpointManager 里程碑方法测试。"""

    def _make_manager(self) -> tuple[CheckpointManager, dict[str, dict]]:
        """构造内存态 Fake DB 的 CheckpointManager。"""
        store: dict[str, dict] = {}
        db = AsyncMock()

        async def _execute(stmt, params=None):
            sql = str(stmt)
            if "INSERT INTO agent_checkpoints" in sql:
                import json

                store[params["session_id"]] = {
                    "agent_state": json.loads(params["state"]),
                    "iteration": params["iteration"],
                }
                return _FakeResult(None)
            if "SELECT agent_state, iteration" in sql:
                row_data = store.get(params["session_id"])
                if row_data is None:
                    return _FakeResult(None)
                return _FakeResult(
                    (row_data["agent_state"], row_data["iteration"])
                )
            return _FakeResult(None)

        db.execute = AsyncMock(side_effect=_execute)
        db.flush = AsyncMock()
        return CheckpointManager(db), store

    @pytest.mark.asyncio
    async def test_save_and_get_milestones(self) -> None:
        """保存后可按序取回里程碑。"""
        mgr, _ = self._make_manager()
        await mgr.save_milestone("s1", "parse", {"status": "done"})
        await mgr.save_milestone("s1", "index", {"status": "done", "duration_ms": 50})

        milestones = await mgr.get_milestones("s1")
        assert [m["name"] for m in milestones] == ["parse", "index"]
        assert milestones[0]["seq"] == 1
        assert milestones[1]["detail"]["duration_ms"] == 50

    @pytest.mark.asyncio
    async def test_get_completed_milestone_names(self) -> None:
        """仅返回 status=done 的里程碑名。"""
        mgr, _ = self._make_manager()
        await mgr.save_milestone("s1", "parse", {"status": "done"})
        await mgr.save_milestone("s1", "index", {"status": "timeout"})
        await mgr.save_milestone("s1", "graph", {"status": "done"})

        completed = await mgr.get_completed_milestone_names("s1")
        assert completed == {"parse", "graph"}

    @pytest.mark.asyncio
    async def test_get_latest_milestone(self) -> None:
        """取最近一条里程碑。"""
        mgr, _ = self._make_manager()
        assert await mgr.get_latest_milestone("s_none") is None
        await mgr.save_milestone("s1", "parse", {"status": "done"})
        await mgr.save_milestone("s1", "index", {"status": "done"})
        latest = await mgr.get_latest_milestone("s1")
        assert latest is not None
        assert latest["name"] == "index"

    @pytest.mark.asyncio
    async def test_get_milestones_empty_when_no_checkpoint(self) -> None:
        """无 checkpoint 时返回空列表。"""
        mgr, _ = self._make_manager()
        assert await mgr.get_milestones("s_none") == []

    @pytest.mark.asyncio
    async def test_save_milestone_with_state_extra(self) -> None:
        """state_extra 合并进 agent_state 顶层。"""
        mgr, store = self._make_manager()
        await mgr.save_milestone(
            "s1", "parse", {"status": "done"}, state_extra={"doc_id": "d1"}
        )
        assert store["s1"]["agent_state"]["doc_id"] == "d1"

    @pytest.mark.asyncio
    async def test_sessions_isolated(self) -> None:
        """不同 session_id 的里程碑相互隔离。"""
        mgr, _ = self._make_manager()
        await mgr.save_milestone("s1", "parse", {"status": "done"})
        await mgr.save_milestone("s2", "extract", {"status": "done"})
        assert [m["name"] for m in await mgr.get_milestones("s1")] == ["parse"]
        assert [m["name"] for m in await mgr.get_milestones("s2")] == ["extract"]


# ======================================================================
# run_stages_with_milestones 测试
# ======================================================================


class _FakeCheckpointManager:
    """内存版 CheckpointManager（仅实现 runner 用到的方法）。"""

    def __init__(self) -> None:
        self.milestones: list[dict] = []

    async def save_milestone(self, key: str, name: str, detail: dict | None = None, **_: object) -> None:
        self.milestones.append({"key": key, "name": name, "detail": detail or {}})

    async def get_milestones(self, key: str) -> list[dict]:
        return [m for m in self.milestones if m["key"] == key]


class TestRunStagesWithMilestones:
    """里程碑阶段执行器测试。"""

    @pytest.mark.asyncio
    async def test_all_stages_run_and_recorded(self) -> None:
        """全部阶段执行并逐阶段记录 done 里程碑。"""
        mgr = _FakeCheckpointManager()
        calls: list[str] = []

        async def _stage(name: str) -> str:
            calls.append(name)
            return f"{name}_result"

        results = await run_stages_with_milestones(
            [
                MilestoneStage("parse", lambda: _stage("parse")),
                MilestoneStage("index", lambda: _stage("index")),
            ],
            task_id="t1",
            checkpoint_manager=mgr,  # type: ignore[arg-type]
            step_timeout_s=10,
        )
        assert calls == ["parse", "index"]
        assert results == {"parse": "parse_result", "index": "index_result"}
        assert [m["name"] for m in mgr.milestones] == ["parse", "index"]
        assert all(m["detail"]["status"] == "done" for m in mgr.milestones)
        # checkpoint key 带 task: 前缀
        assert all(m["key"] == "task:t1" for m in mgr.milestones)

    @pytest.mark.asyncio
    async def test_resume_skips_done_stages(self) -> None:
        """断点恢复：已完成阶段被跳过，只执行未完成阶段。"""
        mgr = _FakeCheckpointManager()
        mgr.milestones.append(
            {"key": "task:t1", "name": "parse", "detail": {"status": "done"}}
        )
        calls: list[str] = []

        async def _stage(name: str) -> str:
            calls.append(name)
            return name

        results = await run_stages_with_milestones(
            [
                MilestoneStage("parse", lambda: _stage("parse")),
                MilestoneStage("index", lambda: _stage("index")),
            ],
            task_id="t1",
            checkpoint_manager=mgr,  # type: ignore[arg-type]
            step_timeout_s=10,
        )
        assert calls == ["index"]
        assert results == {"index": "index"}

    @pytest.mark.asyncio
    async def test_resume_recovers_persisted_results(self) -> None:
        """断点恢复：done 里程碑中持久化的结果被还原，无需重跑。"""
        mgr = _FakeCheckpointManager()
        mgr.milestones.append(
            {
                "key": "task:t1",
                "name": "parse",
                "detail": {"status": "done", "result": {"pages": 5}},
            }
        )
        calls: list[str] = []

        async def _stage(name: str) -> str:
            calls.append(name)
            return {"fresh": True}

        results = await run_stages_with_milestones(
            [MilestoneStage("parse", lambda: _stage("parse"))],
            task_id="t1",
            checkpoint_manager=mgr,  # type: ignore[arg-type]
            step_timeout_s=10,
        )
        assert calls == []  # 未重跑
        assert results == {"parse": {"pages": 5}}  # 结果从里程碑还原

    @pytest.mark.asyncio
    async def test_done_milestone_persists_result(self) -> None:
        """可序列化的阶段结果随 done 里程碑持久化。"""
        mgr = _FakeCheckpointManager()

        async def _stage() -> dict:
            return {"pages": 3}

        await run_stages_with_milestones(
            [MilestoneStage("parse", _stage)],
            task_id="t1",
            checkpoint_manager=mgr,  # type: ignore[arg-type]
            step_timeout_s=10,
        )
        assert mgr.milestones[0]["detail"]["result"] == {"pages": 3}

    @pytest.mark.asyncio
    async def test_resume_disabled_reruns_all(self) -> None:
        """resume=False 时无视历史里程碑全部重跑。"""
        mgr = _FakeCheckpointManager()
        mgr.milestones.append(
            {"key": "task:t1", "name": "parse", "detail": {"status": "done"}}
        )
        calls: list[str] = []

        async def _stage(name: str) -> str:
            calls.append(name)
            return name

        await run_stages_with_milestones(
            [MilestoneStage("parse", lambda: _stage("parse"))],
            task_id="t1",
            checkpoint_manager=mgr,  # type: ignore[arg-type]
            step_timeout_s=10,
            resume=False,
        )
        assert calls == ["parse"]

    @pytest.mark.asyncio
    async def test_step_timeout_records_and_raises(self) -> None:
        """单步超时：记录 timeout 里程碑并抛出 TimeoutError。"""
        mgr = _FakeCheckpointManager()

        async def _slow() -> None:
            await asyncio.sleep(5)

        with pytest.raises((TimeoutError, asyncio.TimeoutError)):
            await run_stages_with_milestones(
                [MilestoneStage("slow_stage", _slow)],
                task_id="t1",
                checkpoint_manager=mgr,  # type: ignore[arg-type]
                step_timeout_s=0.05,
            )
        assert len(mgr.milestones) == 1
        assert mgr.milestones[0]["detail"]["status"] == "timeout"

    @pytest.mark.asyncio
    async def test_timeout_stage_reruns_on_retry(self) -> None:
        """超时阶段无 done 里程碑 — 重试时重新执行。"""
        mgr = _FakeCheckpointManager()
        mgr.milestones.append(
            {"key": "task:t1", "name": "parse", "detail": {"status": "done"}}
        )
        mgr.milestones.append(
            {"key": "task:t1", "name": "index", "detail": {"status": "timeout"}}
        )
        calls: list[str] = []

        async def _stage(name: str) -> str:
            calls.append(name)
            return name

        await run_stages_with_milestones(
            [
                MilestoneStage("parse", lambda: _stage("parse")),
                MilestoneStage("index", lambda: _stage("index")),
            ],
            task_id="t1",
            checkpoint_manager=mgr,  # type: ignore[arg-type]
            step_timeout_s=10,
        )
        assert calls == ["index"]

    @pytest.mark.asyncio
    async def test_stage_exception_propagates_without_done_milestone(self) -> None:
        """阶段异常直接上抛，不写 done 里程碑（重试会重跑该阶段）。"""
        mgr = _FakeCheckpointManager()

        async def _boom() -> None:
            raise ValueError("stage failed")

        with pytest.raises(ValueError, match="stage failed"):
            await run_stages_with_milestones(
                [MilestoneStage("bad", _boom)],
                task_id="t1",
                checkpoint_manager=mgr,  # type: ignore[arg-type]
                step_timeout_s=10,
            )
        assert mgr.milestones == []


# ======================================================================
# 配置测试
# ======================================================================


class TestMilestoneConfig:
    """P2-13 配置项测试。"""

    def test_task_step_timeout_config_exists(self) -> None:
        """Settings 包含 TASK_STEP_TIMEOUT_SECONDS。"""
        from app.config import get_settings

        s = get_settings()
        assert hasattr(s, "TASK_STEP_TIMEOUT_SECONDS")
        assert isinstance(s.TASK_STEP_TIMEOUT_SECONDS, int)
        assert s.TASK_STEP_TIMEOUT_SECONDS > 0
