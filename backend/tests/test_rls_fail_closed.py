"""RLS fail-closed 守卫测试。

这一组守的是「默认拒绝」不被悄悄改回去。三块：

1. 迁移生成的策略文本必须是 fail-closed —— 只要有人把
   ``current_setting(...) IS NULL`` 那个分支加回来，测试立刻红；
2. 所有 Celery 任务必须声明租户身份（显式传参、跨租户理由、或靠 ContextVar），
   不允许出现"无身份还去查库"；
3. ``get_db_session`` 在无租户时**不能**替调用方兜底 —— 兜底等于 fail-open。
"""

from __future__ import annotations

import ast
import importlib.util
import pathlib
from typing import Any

import pytest

BACKEND_DIR = pathlib.Path(__file__).resolve().parents[1]
MIGRATION_FILE = (
    BACKEND_DIR
    / "alembic"
    / "versions"
    / "2026_11_10_1800-e8f9a0b1c2d3_rls_fail_closed.py"
)
TASKS_DIR = BACKEND_DIR / "tasks"


def _load_migration() -> Any:
    spec = importlib.util.spec_from_file_location("rls_fail_closed_mig", MIGRATION_FILE)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ======================================================================
# 一、策略文本
# ======================================================================


class TestPolicyPredicate:
    def test_strict_predicate_rejects_unset_guc(self) -> None:
        """fail-closed 的关键：没有 ``IS NULL`` 兜底分支。

        ``IS NULL OR ...`` 就是原漏洞本身 —— GUC 未设置时第一个分支恒真，
        全表可见。这条断言是整组测试里最该守的一条。
        """
        mod = _load_migration()
        pred = mod._STRICT_PREDICATE
        assert (
            "IS NULL" not in pred.upper()
        ), "严格策略里出现了 IS NULL 分支 —— fail-open 又回来了"
        assert "app.tenant_id" in pred
        assert "''*''" in pred, "跨租户哨兵分支丢了（system_scope 会失效）"

    def test_legacy_predicate_only_used_in_downgrade(self) -> None:
        """fail-open 版本只能出现在 downgrade 里。"""
        mod = _load_migration()
        assert "IS NULL" in mod._LEGACY_PREDICATE.upper()

        src = MIGRATION_FILE.read_text(encoding="utf-8")
        upgrade_body = src.split("def upgrade() -> None:")[1].split(
            "def downgrade() -> None:"
        )[0]
        assert (
            "_LEGACY_PREDICATE" not in upgrade_body
        ), "upgrade 里出现了 fail-open 谓词"

    def test_comparison_uses_text_cast(self) -> None:
        """必须用 ``tenant_id::text`` 比较，不能用 ``::uuid``。

        GUC 为 ``'*'`` 时 ``'*'::uuid`` 会直接抛
        ``invalid input syntax for type uuid``，策略变成报错而非放行，
        跨租户维护脚本全部失败。
        """
        pred = _load_migration()._STRICT_PREDICATE
        assert "tenant_id::text" in pred
        assert "::uuid" not in pred


# ======================================================================
# 二、Celery 任务身份覆盖
# ======================================================================

_TENANT_APIS = {
    "apply_tenant",
    "resolve_task_tenant",
    "resolve_tenant_of_row",
    "tenant_scope",
    "system_scope",
}


def _dep_name(node: ast.AST) -> str | None:
    if not isinstance(node, ast.Call):
        return None
    return getattr(node.func, "id", None) or getattr(node.func, "attr", None)


def _scan_task_sessions() -> list[tuple[str, int, str, str]]:
    """返回 ``[(文件, 行号, 函数, 状态)]``，状态 ∈ {OK, CONTEXT, MISSING}。"""
    rows: list[tuple[str, int, str, str]] = []
    for path in sorted(TASKS_DIR.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for func in ast.walk(tree):
            if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for node in ast.walk(func):
                if not isinstance(node, ast.AsyncWith):
                    continue
                for item in node.items:
                    call = item.context_expr
                    if _dep_name(call) != "task_db_session":
                        continue
                    assert isinstance(call, ast.Call)
                    declared = bool(call.args) or any(
                        kw.arg in ("tenant_id", "cross_tenant_reason")
                        for kw in call.keywords
                    )
                    body_uses = any(
                        _dep_name(inner) in _TENANT_APIS
                        for stmt in node.body
                        for inner in ast.walk(stmt)
                    )
                    has_param = "tenant_id" in {
                        a.arg for a in list(func.args.args) + list(func.args.kwonlyargs)
                    }
                    if declared or body_uses:
                        status = "OK"
                    elif has_param:
                        status = "CONTEXT"
                    else:
                        status = "MISSING"
                    rows.append((path.name, node.lineno, func.name, status))
    return rows


class TestCeleryTaskIdentity:
    def test_every_task_session_declares_identity(self) -> None:
        """没有任何 ``task_db_session()`` 处于"既无参数、又无解析、又无跨租户理由"的状态。

        fail-closed 之后这种写法不会报错 —— 它只会安静地查不到任何数据，
        表现为"任务跑完了但什么都没做"。这条断言把它变成 CI 红灯。
        """
        missing = [r for r in _scan_task_sessions() if r[3] == "MISSING"]
        detail = "\n".join(f"  {f}:{ln} in {fn}()" for f, ln, fn, _ in missing)
        assert not missing, (
            f"{len(missing)} 处任务会话未声明租户身份"
            f"（用 tenant_id= / cross_tenant_reason= / resolve_task_tenant 之一）：\n{detail}"
        )

    def test_cross_tenant_tasks_carry_reason(self) -> None:
        """跨租户的任务必须写理由 —— 否则审计日志里只有一堆 ``(empty)``。"""
        offenders: list[str] = []
        for path in sorted(TASKS_DIR.glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                if _dep_name(node) != "task_db_session":
                    continue
                for kw in node.keywords:
                    if kw.arg != "cross_tenant_reason":
                        continue
                    if not (isinstance(kw.value, ast.Constant) and kw.value.value):
                        offenders.append(f"{path.name}:{node.lineno}")
        assert not offenders, "cross_tenant_reason 必须是非空字面量：\n" + "\n".join(
            offenders
        )


# ======================================================================
# 三、上下文注入 API
# ======================================================================


class _Recorder:
    """记录 ``set_config`` 调用的假会话。"""

    def __init__(self) -> None:
        self.values: list[str] = []

    async def execute(self, _stmt: Any, params: dict[str, Any] | None = None) -> None:
        if params and "value" in params:
            self.values.append(params["value"])


class TestTenantContextApi:
    def test_normalize_rejects_empty(self) -> None:
        from app.core.tenant_context import TenantContextError, normalize_tenant_id

        for bad in (None, "", "   "):
            with pytest.raises(TenantContextError):
                normalize_tenant_id(bad)

    def test_normalize_rejects_cross_tenant_sentinel(self) -> None:
        """``'*'`` 只能由 system_scope 写入，不能从普通入口混进来。"""
        from app.core.tenant_context import TenantContextError, normalize_tenant_id

        with pytest.raises(TenantContextError):
            normalize_tenant_id("*")

    def test_normalize_lowercases_uuid(self) -> None:
        from app.core.tenant_context import normalize_tenant_id

        assert (
            normalize_tenant_id("AAAAAAAA-0000-4000-8000-000000000001")
            == "aaaaaaaa-0000-4000-8000-000000000001"
        )

    @pytest.mark.asyncio
    async def test_system_scope_requires_reason(self) -> None:
        from app.core.tenant_context import TenantContextError, system_scope

        with pytest.raises(TenantContextError):
            async with system_scope(_Recorder(), reason="   "):
                pass

    @pytest.mark.asyncio
    async def test_system_scope_sets_and_clears_sentinel(self) -> None:
        from app.core.tenant_context import CROSS_TENANT, system_scope

        rec = _Recorder()
        async with system_scope(rec, reason="pytest"):
            assert rec.values[-1] == CROSS_TENANT
        assert rec.values[-1] == "", "退出作用域后必须清空，否则连接复用会串租户"

    @pytest.mark.asyncio
    async def test_tenant_scope_roundtrip(self) -> None:
        from app.core.tenant_context import tenant_scope

        rec = _Recorder()
        tid = "aaaaaaaa-0000-4000-8000-000000000001"
        async with tenant_scope(rec, tid) as got:
            assert got == tid
            assert rec.values[-1] == tid
        assert rec.values[-1] == ""


# ======================================================================
# 四、请求依赖
# ======================================================================


class _FakeSession:
    def __init__(self) -> None:
        self.values: list[str] = []
        self.closed = False
        self.committed = 0

    async def execute(self, _stmt: Any, params: dict[str, Any] | None = None) -> None:
        if params and "value" in params:
            self.values.append(params["value"])

    async def commit(self) -> None:
        self.committed += 1

    async def rollback(self) -> None:
        pass

    async def close(self) -> None:
        self.closed = True


def _patch_session_factory(monkeypatch: pytest.MonkeyPatch) -> _FakeSession:
    from contextlib import asynccontextmanager

    import app.database as db_mod

    fake = _FakeSession()

    @asynccontextmanager
    async def _factory():
        yield fake

    monkeypatch.setattr(db_mod, "async_session_factory", _factory)
    return fake


class _Req:
    def __init__(self, tenant_id: Any = None) -> None:
        self.state = type("S", (), {"tenant_id": tenant_id})()
        self.url = type("U", (), {"path": "/pytest"})()
        self.method = "GET"


class TestRequestDependency:
    @pytest.mark.asyncio
    async def test_no_tenant_sets_no_guc(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """无租户 → 一个 GUC 都不设。

        这就是"不兜底"：策略 fail-closed，一行都查不到。如果这里改成
        "设成 '*'"，登录页之后的匿名接口会静默看到全租户数据。
        """
        from app.database import get_db_session

        fake = _patch_session_factory(monkeypatch)
        agen = get_db_session(_Req(tenant_id=None))
        await anext(agen)

        # 进入会话时一个 GUC 都不该写 —— 这是"不兜底"的核心。
        # （退出时会清一次空串，那是安全方向，不算兜底。）
        assert fake.values == [], f"无租户时不应写 GUC，实际写了 {fake.values}"

        await agen.aclose()
        assert [v for v in fake.values if v] == []

    @pytest.mark.asyncio
    async def test_tenant_is_applied_and_cleared(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from app.database import get_db_session

        fake = _patch_session_factory(monkeypatch)
        tid = "aaaaaaaa-0000-4000-8000-000000000001"
        agen = get_db_session(_Req(tenant_id=tid))
        await anext(agen)
        assert fake.values == [tid]
        await agen.aclose()
        assert fake.values[-1] == "", "请求结束必须清空 GUC"
        assert fake.closed

    @pytest.mark.asyncio
    async def test_system_dependency_sets_sentinel(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from app.core.tenant_context import CROSS_TENANT
        from app.database import get_system_db_session

        fake = _patch_session_factory(monkeypatch)
        agen = get_system_db_session(_Req())
        await anext(agen)
        assert fake.values == [CROSS_TENANT]
        await agen.aclose()

    @pytest.mark.asyncio
    async def test_invalid_tenant_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """非法 tenant_id 必须炸，不能被当成"没有租户"静默放行。"""
        from app.core.tenant_context import TenantContextError
        from app.database import get_db_session

        _patch_session_factory(monkeypatch)
        agen = get_db_session(_Req(tenant_id="not-a-uuid"))
        with pytest.raises(TenantContextError):
            await anext(agen)
