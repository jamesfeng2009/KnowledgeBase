"""
Celery 队列配置回归测试 — 防止任务静默堆积的配置回退。

历史问题：原实现把 ``queues=[...]`` 传给 Celery() 构造函数，
该参数不是有效配置键（仅存入 preconf 不映射为 task_queues），
导致 task_queues 为空、worker 只消费默认 "celery" 队列，
路由到命名队列的任务全部堆积无人消费。

本测试通过直接断言 celery_app.conf 的有效配置，
确保以下不变量始终成立：
1. task_queues 显式声明全部业务队列（含默认兜底队列）
2. include 中的每个任务模块都有匹配的 task_routes 规则
3. task_routes 指向的队列必须存在于 task_queues 中
4. 可靠性配置（acks_late / reject_on_worker_lost / visibility_timeout）
5. beat_schedule 中的任务都能路由到已声明的队列
6. docker-compose worker 命令通过 -Q 显式消费全部队列（双重保障）

不依赖外部服务（Redis/DB），纯配置断言，可在任意环境运行。
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import pytest

# backend/ 目录（tests/ 的上一级）
BACKEND_ROOT = Path(__file__).parent.parent
# 项目根目录（backend/ 的上一级）
PROJECT_ROOT = BACKEND_ROOT.parent


def _load_real_celery_app():
    """从源码文件加载真实 celery_app 模块。

    套件内多个测试文件在收集期向 sys.modules["celery"] 和
    sys.modules["celery_app"] 塞入 MagicMock（避免模块级副作用），
    直接 ``from celery_app import ...`` 在全量运行时拿到的是 Mock
    而非真实配置，断言全部失真。

    处理方式：加载前临时弹出 MagicMock 污染项，以独立模块名从源码
    执行真实 celery_app.py；加载后仅恢复 celery_app 的 MagicMock
    （其他测试文件对该模块的 mock 语义不变），保留真实 celery 包
    在 sys.modules 中 — celery 在本环境真实安装，套件内各文件的
    mock 仅为"防未安装"兜底且从不引用 mock 对象本身，真实包可
    满足其传递导入；同时 celery_app.conf 属性访问在运行期需要
    懒导入 celery.loaders 等子模块，必须保持真实 celery 可用。
    """
    import sys
    from unittest.mock import NonCallableMock

    path = BACKEND_ROOT / "celery_app.py"

    # 弹出 MagicMock 污染（celery / celery_app），让真实模块可导入
    popped_celery_app = None
    for name in ("celery", "celery_app"):
        mod = sys.modules.get(name)
        if mod is not None and isinstance(mod, NonCallableMock):
            popped = sys.modules.pop(name)
            if name == "celery_app":
                popped_celery_app = popped
    try:
        spec = importlib.util.spec_from_file_location(
            "_real_celery_app_for_config_test", path
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        # 恢复 celery_app 的 MagicMock 污染；保留真实 celery 包
        if popped_celery_app is not None:
            sys.modules["celery_app"] = popped_celery_app


_real = _load_real_celery_app()
celery_app = _real.celery_app
_ALL_QUEUES = _real._ALL_QUEUES


class TestTaskQueues:
    """task_queues 必须显式声明全部业务队列。"""

    def test_task_queues_not_empty(self):
        """task_queues 配置非空（回归：queues= 构造函数参数不生效）。"""
        queues = celery_app.conf.task_queues
        assert queues, "task_queues 为空 — worker 将只消费默认队列"

    def test_all_business_queues_declared(self):
        """全部 7 个业务队列都已声明。"""
        declared = {q.name for q in celery_app.conf.task_queues}
        expected = set(_ALL_QUEUES)
        assert expected <= declared, (
            f"缺失队列: {expected - declared}"
        )

    def test_default_queue_in_task_queues(self):
        """默认队列 'celery' 必须在 task_queues 中（兜底消费）。"""
        declared = {q.name for q in celery_app.conf.task_queues}
        assert "celery" in declared

    def test_task_default_queue(self):
        """task_default_queue 必须是 'celery'。"""
        assert celery_app.conf.task_default_queue == "celery"

    def test_dead_letter_queue_declared(self):
        """死信队列必须声明（重试耗尽任务的兜底）。"""
        declared = {q.name for q in celery_app.conf.task_queues}
        assert "dead_letter" in declared


class TestTaskRoutes:
    """task_routes 必须覆盖 include 中的全部任务模块。"""

    def test_routes_not_empty(self):
        """task_routes 配置非空。"""
        assert celery_app.conf.task_routes, "task_routes 为空"

    def test_all_included_modules_have_routes(self):
        """include 中的每个任务模块都必须有路由规则。

        未匹配的模块会落入默认队列兜底，但显式路由可以避免
        重任务（如视频处理）阻塞默认队列。
        """
        routes = celery_app.conf.task_routes
        # include 列表从 celery_app.conf 读取（Celery 会归一化）
        included = set(celery_app.conf.include or [])
        assert included, "include 列表为空"

        missing = []
        for module in included:
            pattern = f"{module}.*"
            if pattern not in routes:
                missing.append(module)
        assert not missing, (
            f"以下任务模块缺少 task_routes 路由规则: {missing}"
        )

    def test_route_targets_are_declared_queues(self):
        """路由目标队列必须存在于 task_queues 中（防笔误）。"""
        declared = {q.name for q in celery_app.conf.task_queues}
        routes = celery_app.conf.task_routes
        for pattern, route in routes.items():
            queue = route.get("queue")
            assert queue in declared, (
                f"路由 {pattern} 指向未声明的队列: {queue}"
            )

    def test_heavy_tasks_not_on_default_queue(self):
        """重任务模块不得路由到默认队列（防阻塞兜底通道）。"""
        routes = celery_app.conf.task_routes
        heavy_modules = [
            "tasks.video_tasks.*",
            "tasks.multimodal_tasks.*",
            "tasks.document_tasks.*",
        ]
        for pattern in heavy_modules:
            queue = routes[pattern]["queue"]
            assert queue != "celery", (
                f"{pattern} 路由到默认队列，重任务会阻塞兜底通道"
            )


class TestReliabilityConfig:
    """可靠性配置 — 任务不丢失、worker 崩溃可恢复。"""

    def test_task_acks_late_enabled(self):
        """task_acks_late 必须开启（worker 崩溃时任务重投）。"""
        assert celery_app.conf.task_acks_late is True

    def test_task_reject_on_worker_lost(self):
        """task_reject_on_worker_lost 必须开启（OOM 强杀时重投）。"""
        assert celery_app.conf.task_reject_on_worker_lost is True

    def test_visibility_timeout_covers_long_tasks(self):
        """broker 可见性超时必须 >= task_time_limit。

        若可见性超时短于任务硬超时，长任务执行中会被 broker
        重新投递，导致同一任务被多个 worker 并发执行。
        """
        transport_opts = celery_app.conf.broker_transport_options or {}
        visibility = transport_opts.get("visibility_timeout", 0)
        time_limit = celery_app.conf.task_time_limit or 0
        assert visibility >= time_limit, (
            f"visibility_timeout({visibility}) < task_time_limit({time_limit})，"
            "长任务会被 broker 重复投递"
        )

    def test_prefetch_multiplier_is_one(self):
        """预取数为 1，避免长任务阻塞短任务。"""
        assert celery_app.conf.worker_prefetch_multiplier == 1

    def test_result_expires_set(self):
        """结果过期时间已配置（防 Redis 内存膨胀）。"""
        assert (celery_app.conf.result_expires or 0) > 0


class TestBeatSchedule:
    """beat_schedule 中的任务必须能路由到已声明队列。"""

    def test_beat_schedule_not_empty(self):
        """定时任务表非空。"""
        assert celery_app.conf.beat_schedule, "beat_schedule 为空"

    def test_beat_tasks_routable_to_declared_queues(self):
        """每个 beat 任务都能通过 task_routes 匹配到已声明队列。"""
        declared = {q.name for q in celery_app.conf.task_queues}
        routes = celery_app.conf.task_routes
        default_queue = celery_app.conf.task_default_queue

        for name, entry in celery_app.conf.beat_schedule.items():
            task_name = entry["task"]
            # 模拟 Celery 路由匹配：找第一个匹配的路由规则
            matched_queue = default_queue
            for pattern, route in routes.items():
                # 路由 pattern 形如 "tasks.xxx_tasks.*"
                prefix = pattern[:-2]  # 去掉 ".*"
                if task_name.startswith(prefix):
                    matched_queue = route["queue"]
                    break
            assert matched_queue in declared, (
                f"beat 任务 {name}({task_name}) 路由到未声明队列 "
                f"{matched_queue}"
            )


class TestDockerComposeWorkerCommand:
    """docker-compose worker 命令必须显式消费全部队列（双重保障）。"""

    def test_worker_command_declares_all_queues(self):
        """celery-worker 的 -Q 参数覆盖全部业务队列。"""
        compose_file = PROJECT_ROOT / "docker-compose.yml"
        assert compose_file.exists(), "docker-compose.yml 不存在"

        content = compose_file.read_text(encoding="utf-8")
        # 提取 celery-worker 服务的 command 行
        match = re.search(
            r"celery-worker:.*?command:\s*(.+?)(?:\n\s*\w|$)",
            content,
            re.DOTALL,
        )
        assert match, "docker-compose.yml 中未找到 celery-worker command"
        command = match.group(1)

        # 必须包含 -Q 参数
        q_match = re.search(r"-Q\s+(\S+)", command)
        assert q_match, (
            "celery-worker command 缺少 -Q 参数 — "
            "即使 task_queues 配置回归也能消费全部队列的双重保障缺失"
        )

        consumed = set(q_match.group(1).split(","))
        expected = set(_ALL_QUEUES)
        assert expected <= consumed, (
            f"worker -Q 参数未覆盖全部队列，缺失: {expected - consumed}"
        )
