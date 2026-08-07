"""
推荐模型重建任务与预计算消费测试 — 覆盖：

- tasks/recommendation_tasks.rebuild_recommendation_model：
  按租户统计 UserBehavior，Redis key 含 tenant_id，矩阵/偏好向量结构正确
  （mock DB / Redis / Embedder）；
- RecommendationService：_load_all_behaviors / _vector_content_recall 优先读
  Redis 预计算结果，未命中时回退现算；
- POST /recommendations/rebuild：提交真实 Celery 任务并返回 task_id
  （mock Celery delay）。

mock 风格参照 test_video_rag.py（celery_app 桩 + exec 加载真实任务模块）
与 test_recommendation_api.py（httpx ASGI + dependency_overrides）。
不依赖外部服务（Redis/DB 全部 mock），可在任意环境运行。
"""

from __future__ import annotations

import json
import sys
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio

from app.services.recommendation_service import RecommendationService

#: 与 tasks/recommendation_tasks._MODEL_TTL_SECONDS 保持一致（7 天）
_MODEL_TTL_SECONDS = 7 * 24 * 3600


# ======================================================================
# Celery 桩与任务模块加载（参照 test_video_rag.py）
# ======================================================================


class _FakeBoundTask:
    """bind=True 任务注入的 self 桩 — retry 直接抛出原异常。"""

    def __init__(self) -> None:
        self.request = SimpleNamespace(retries=0)
        self.max_retries = 3

    def retry(self, exc: Exception | None = None) -> None:
        raise exc if exc is not None else Exception("retry")


class _CeleryTaskStub:
    """celery_app.task 装饰器桩 — 保留原函数使其可直接调用。"""

    @staticmethod
    def task(*_args, **kwargs):
        def _decorator(fn):
            if not kwargs.get("bind"):
                return fn

            import functools

            @functools.wraps(fn)
            def _bound(*args, **kw):
                return fn(_FakeBoundTask(), *args, **kw)

            return _bound

        return _decorator


def _load_recommendation_tasks_module():
    """以受控方式加载真实的 tasks/recommendation_tasks.py（celery_app 打桩）。

    套件中 celery_app 可能被注入 MagicMock；此处用桩 celery_app exec
    源码加载独立模块对象，拿到可直接调用的 rebuild_recommendation_model。
    """
    import types as _types
    from pathlib import Path

    stub_celery_app = _types.ModuleType("celery_app")
    stub_celery_app.celery_app = _CeleryTaskStub()

    path = Path(__file__).resolve().parent.parent / "tasks" / "recommendation_tasks.py"
    source = path.read_text(encoding="utf-8")
    module = _types.ModuleType("ekb_test_recommendation_tasks")
    module.__file__ = str(path)

    saved = sys.modules.get("celery_app")
    sys.modules["celery_app"] = stub_celery_app
    try:
        exec(compile(source, str(path), "exec"), module.__dict__)
    finally:
        if saved is not None:
            sys.modules["celery_app"] = saved
        else:
            sys.modules.pop("celery_app", None)
    return module


# ======================================================================
# DB / Redis / Embedder 桩
# ======================================================================


class _FakeResult:
    """SQLAlchemy Result 桩 — 支持 .scalars().all() 链式调用。"""

    def __init__(self, rows: list) -> None:
        self._rows = rows

    def scalars(self) -> "_FakeResult":
        return self

    def all(self) -> list:
        return list(self._rows)


class _FakeSession:
    """AsyncSession 桩 — 按调用顺序依次返回预置的查询结果。"""

    def __init__(self, results: list[list]) -> None:
        self._results = list(results)
        self.execute_calls = 0

    async def execute(self, _stmt) -> _FakeResult:
        idx = min(self.execute_calls, len(self._results) - 1)
        self.execute_calls += 1
        return _FakeResult(self._results[idx])


class _FakeSyncRedis:
    """同步 Redis 客户端桩 — 捕获 setex 写入。"""

    def __init__(self) -> None:
        self.writes: dict[str, tuple[int, str]] = {}
        self.closed = False

    def setex(self, key: str, ttl: int, value: str) -> None:
        self.writes[key] = (ttl, value)

    def close(self) -> None:
        self.closed = True


class _FakeAsyncRedis:
    """异步缓存桩 — 注入 RecommendationService(cache=...)。"""

    def __init__(self, data: dict[str, str] | None = None) -> None:
        self.data = dict(data or {})

    async def get(self, key: str):
        return self.data.get(key)

    async def set(self, key: str, value: str, ttl: int | None = None) -> None:
        self.data[key] = value


class _FakeEmbedder:
    """Embedder 桩 — 记录调用并返回固定向量。"""

    def __init__(self, vector: tuple[float, ...] = (0.1, 0.2, 0.3)) -> None:
        self.vector = list(vector)
        self.calls: list[list[str]] = []

    async def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        return [list(self.vector) for _ in texts]


class _FailEmbedder:
    """Embedder 桩 — 被调用即失败（用于证明预计算命中时不现算）。"""

    async def embed(self, texts: list[str]) -> list[list[float]]:
        raise AssertionError("预计算命中时不应调用 embed 现算")


class _FakeVectorStore:
    """向量库桩 — 记录检索向量并返回固定结果。"""

    def __init__(self, results: list[dict] | None = None) -> None:
        self.results = results or [
            {"doc_id": str(uuid4()), "title": "命中文档", "score": 0.9}
        ]
        self.searched: list[list[float]] = []

    async def search(self, vec, kb_ids=None, top_k: int = 10) -> list[dict]:
        self.searched.append(list(vec))
        return list(self.results)


def _make_behavior(user_id: uuid.UUID, doc_id: uuid.UUID, weight: float = 1.0):
    """构造 UserBehavior 桩对象（任务只访问 user_id/doc_id/weight）。"""
    return SimpleNamespace(
        user_id=user_id,
        doc_id=doc_id,
        weight=weight,
        acted_at=datetime.now(timezone.utc),
    )


def _run_task(module, fake_session: _FakeSession, fake_redis: _FakeSyncRedis,
              embedder: Any, **kwargs):
    """在受控桩环境下执行 rebuild_recommendation_model。"""

    @asynccontextmanager
    async def fake_task_db_session():
        yield fake_session

    with patch("app.database.task_db_session", fake_task_db_session), \
         patch("redis.from_url", return_value=fake_redis), \
         patch("app.llm.embedder.get_embedder", return_value=embedder):
        return module.rebuild_recommendation_model(**kwargs)


# ======================================================================
# 任务测试：Redis key 含 tenant_id 且结构正确
# ======================================================================


class TestRebuildRecommendationModel:
    """rebuild_recommendation_model 任务测试。"""

    def test_writes_matrix_and_prefvec_with_tenant_key(self):
        """指定租户重建：cf:matrix 与 prefvec key 含 tenant_id，结构正确。"""
        module = _load_recommendation_tasks_module()
        tid = uuid4()
        u1, u2 = uuid4(), uuid4()
        d1, d2 = uuid4(), uuid4()

        behaviors = [
            _make_behavior(u1, d1, 2.0),
            _make_behavior(u1, d2, 1.0),
            _make_behavior(u2, d1, 3.0),
        ]
        docs = [
            SimpleNamespace(id=d1, title="文档一", deleted_at=None),
            SimpleNamespace(id=d2, title="文档二", deleted_at=None),
        ]
        # 指定 tenant_id：跳过 Tenant 查询，第 1 次 execute 查行为、第 2 次查文档
        session = _FakeSession([behaviors, docs])
        redis_client = _FakeSyncRedis()
        embedder = _FakeEmbedder()

        result = _run_task(module, session, redis_client, embedder, tenant_id=str(tid))

        assert result["status"] == "success"
        assert result["tenants"] == 1
        assert result["behavior_rows"] == 3
        assert result["preference_vectors"] == 2

        # 协同过滤矩阵：key 含 tenant_id，结构为 {user_id: {doc_id: weight}}
        matrix_key = f"recommend:{tid}:cf:matrix"
        assert matrix_key in redis_client.writes
        ttl, raw = redis_client.writes[matrix_key]
        assert ttl == _MODEL_TTL_SECONDS
        matrix = json.loads(raw)
        assert matrix == {
            str(u1): {str(d1): 2.0, str(d2): 1.0},
            str(u2): {str(d1): 3.0},
        }

        # 用户偏好向量：key 含 tenant_id + user_id，值为向量数组
        for uid in (u1, u2):
            pref_key = f"recommend:{tid}:prefvec:{uid}"
            assert pref_key in redis_client.writes
            ttl, raw = redis_client.writes[pref_key]
            assert ttl == _MODEL_TTL_SECONDS
            assert json.loads(raw) == [0.1, 0.2, 0.3]

        # embed 按用户批量调用（2 个用户 → 1 批 2 条文本）
        assert embedder.calls == [["文档一 文档二", "文档一"]]
        assert redis_client.closed is True

    def test_full_rebuild_iterates_tenants(self):
        """不传 tenant_id：遍历全部租户，逐租户写隔离的 key。"""
        module = _load_recommendation_tasks_module()
        t1, t2 = uuid4(), uuid4()
        u1, d1 = uuid4(), uuid4()

        tenants = [SimpleNamespace(id=t1), SimpleNamespace(id=t2)]
        behaviors = [_make_behavior(u1, d1)]
        docs = [SimpleNamespace(id=d1, title="文档", deleted_at=None)]
        # 调用顺序：Tenant 查询 → t1 行为 → t1 文档 → t2 行为 → t2 文档
        session = _FakeSession([tenants, behaviors, docs, behaviors, docs])
        redis_client = _FakeSyncRedis()

        result = _run_task(module, session, redis_client, _FakeEmbedder())

        assert result["status"] == "success"
        assert result["tenants"] == 2
        # 两个租户各自写入隔离的矩阵 key
        assert f"recommend:{t1}:cf:matrix" in redis_client.writes
        assert f"recommend:{t2}:cf:matrix" in redis_client.writes

    def test_matrix_only_when_embedder_unavailable(self):
        """Embedder 不可用时仍写矩阵、跳过偏好向量（优雅降级）。"""
        module = _load_recommendation_tasks_module()
        tid = uuid4()
        u1, d1 = uuid4(), uuid4()

        behaviors = [_make_behavior(u1, d1)]
        session = _FakeSession([behaviors])
        redis_client = _FakeSyncRedis()

        @asynccontextmanager
        async def fake_task_db_session():
            yield session

        with patch("app.database.task_db_session", fake_task_db_session), \
             patch("redis.from_url", return_value=redis_client), \
             patch("app.llm.embedder.get_embedder", side_effect=RuntimeError("无 embedder")):
            result = module.rebuild_recommendation_model(tenant_id=str(tid))

        assert result["status"] == "success"
        assert result["preference_vectors"] == 0
        assert f"recommend:{tid}:cf:matrix" in redis_client.writes
        assert not any("prefvec" in k for k in redis_client.writes)

    def test_failure_raises_for_retry(self):
        """DB 异常时任务抛错（bind=True 下走 self.retry → 测试桩重抛原异常）。"""
        module = _load_recommendation_tasks_module()

        class _BoomSession:
            async def execute(self, _stmt):
                raise ConnectionError("db down")

        @asynccontextmanager
        async def fake_task_db_session():
            yield _BoomSession()

        with patch("app.database.task_db_session", fake_task_db_session), \
             patch("redis.from_url", return_value=_FakeSyncRedis()):
            with pytest.raises(ConnectionError):
                module.rebuild_recommendation_model(tenant_id=str(uuid4()))


# ======================================================================
# 服务测试：优先读预计算，未命中回退现算
# ======================================================================


class TestPrecomputedCfMatrix:
    """_load_all_behaviors 优先读 Redis 预计算矩阵。"""

    async def test_prefers_precomputed_matrix(self):
        """预计算命中：直接返回 Redis 矩阵，不查 DB。"""
        matrix = {"u1": {"d1": 2.0}, "u2": {"d1": 1.0, "d2": 3.0}}
        cache = _FakeAsyncRedis({"recommend:default:cf:matrix": json.dumps(matrix)})
        db = AsyncMock()
        svc = RecommendationService(db, cache=cache)

        result = await svc._load_all_behaviors()

        assert result == matrix
        db.execute.assert_not_called()

    async def test_falls_back_to_db_when_miss(self):
        """预计算未命中：回退 DB 现算，聚合权重一致。"""
        cache = _FakeAsyncRedis()
        u1, d1, d2 = uuid4(), uuid4(), uuid4()
        rows = [
            _make_behavior(u1, d1, 1.0),
            _make_behavior(u1, d1, 2.0),  # 同 doc 累加
            _make_behavior(u1, d2, 3.0),
        ]
        db = AsyncMock()
        db.execute = AsyncMock(return_value=_FakeResult(rows))
        svc = RecommendationService(db, cache=cache)

        result = await svc._load_all_behaviors()

        assert result == {str(u1): {str(d1): 3.0, str(d2): 3.0}}
        db.execute.assert_called_once()


class TestPrecomputedPreferenceVector:
    """_vector_content_recall 优先读 Redis 预计算偏好向量。"""

    async def test_prefers_precomputed_vector(self):
        """预计算命中：跳过标题聚合与 embed，直接用预计算向量检索。"""
        uid, did = uuid4(), uuid4()
        behaviors = [_make_behavior(uid, did)]
        cache = _FakeAsyncRedis(
            {f"recommend:default:prefvec:{uid}": json.dumps([0.1, 0.2, 0.3])}
        )
        store = _FakeVectorStore()
        svc = RecommendationService(
            AsyncMock(),
            embedder=_FailEmbedder(),
            vector_store=store,
            cache=cache,
        )
        # 命中预计算时不应触发标题查询
        svc._fetch_docs = AsyncMock(side_effect=AssertionError("不应现算标题"))

        result = await svc._vector_content_recall(behaviors, top_k=5)

        assert store.searched == [[0.1, 0.2, 0.3]]
        assert result[0]["reason"] == "vector"
        assert result[0]["doc_id"] == store.results[0]["doc_id"]

    async def test_falls_back_to_compute_when_miss(self):
        """预计算未命中：回退现算（取标题 → embed → 检索）。"""
        uid, did = uuid4(), uuid4()
        behaviors = [_make_behavior(uid, did)]
        cache = _FakeAsyncRedis()
        embedder = _FakeEmbedder(vector=(0.4, 0.5))
        store = _FakeVectorStore()
        svc = RecommendationService(
            AsyncMock(), embedder=embedder, vector_store=store, cache=cache
        )
        svc._fetch_docs = AsyncMock(
            return_value={str(did): SimpleNamespace(title="标题A")}
        )

        result = await svc._vector_content_recall(behaviors, top_k=5)

        assert embedder.calls == [["标题A"]]
        assert store.searched == [[0.4, 0.5]]
        assert result[0]["reason"] == "vector"


# ======================================================================
# 端点测试：/rebuild 返回真实 task_id
# ======================================================================


@pytest_asyncio.fixture
async def admin_client():
    """带管理员认证与 DB 覆盖的客户端。"""
    from app.database import get_db_session
    from app.deps import get_current_user
    from app.main import app
    from app.middleware import get_rate_limiter

    # 清理限流器 buckets，防止跨测试触发 429
    limiter = get_rate_limiter()
    if limiter is not None:
        limiter.clear()

    mock_user = SimpleNamespace(
        id=uuid4(), role="admin", is_active=True,
        email="admin@test.com", name="管理员",
    )

    async def override_user():
        return mock_user

    async def override_db():
        yield AsyncMock()

    app.dependency_overrides[get_current_user] = override_user
    app.dependency_overrides[get_db_session] = override_db

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        yield client

    app.dependency_overrides.clear()


class TestRebuildEndpoint:
    """POST /api/v1/recommendations/rebuild 测试。"""

    async def test_rebuild_returns_real_task_id(
        self, admin_client: httpx.AsyncClient
    ) -> None:
        """管理员调用：提交 Celery 任务并返回真实 task_id。"""
        mock_task = MagicMock()
        mock_task.delay = MagicMock(
            return_value=SimpleNamespace(id="celery-task-abc123")
        )

        with patch(
            "tasks.recommendation_tasks.rebuild_recommendation_model", mock_task
        ):
            response = await admin_client.post("/api/v1/recommendations/rebuild")

        assert response.status_code == 200
        data = response.json()
        assert data["code"] == 0
        assert data["data"]["status"] == "queued"
        assert data["data"]["task_id"] == "celery-task-abc123"
        # 测试请求未携带租户上下文 → 全量重建
        mock_task.delay.assert_called_once_with(tenant_id=None)

    async def test_rebuild_submit_failure_returns_500(
        self, admin_client: httpx.AsyncClient
    ) -> None:
        """broker 不可用时返回明确错误而非未处理异常。"""
        mock_task = MagicMock()
        mock_task.delay = MagicMock(side_effect=ConnectionError("broker down"))

        with patch(
            "tasks.recommendation_tasks.rebuild_recommendation_model", mock_task
        ):
            response = await admin_client.post("/api/v1/recommendations/rebuild")

        assert response.status_code == 200
        data = response.json()
        assert data["code"] == 500
        assert "重建任务提交失败" in data["message"]
