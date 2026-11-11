"""测试环境自举门禁 — 守住两个「换台机器跑就炸」的坑。

这两个问题都不会以「断言失败」的形式出现，而是以「上百个 ERROR + 报错信息
指向无关位置」的形式出现，排查成本极高（已各自误判过一轮）。所以单独用测试
把行为钉住：

1. pytest 临时根目录隔离 —— 否则会出现近百个 FileExistsError，堆栈落在
   pytest 内部的 basetemp.mkdir()，与项目代码毫无关系。
2. 测试库端口自动探测 —— `.env` 写的是 compose 设计值（15432），机器上实际
   在跑的可能是手工容器（55432），写错会一次性产生两百多个 Connection refused。
"""
from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]
TESTS_DIR = BACKEND_DIR / "tests"


def _load(name: str, path: Path):
    """按文件路径加载模块（tests/ 与 scripts/ 都不是包，不能直接 import）。"""
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def conftest_mod():
    return _load("ekb_tests_conftest", TESTS_DIR / "conftest.py")


# ---------------------------------------------------------------------------
# 一、pytest 临时根目录
# ---------------------------------------------------------------------------


class TestPytestTemprootIsolation:
    def test_temproot_points_to_fresh_directory(self) -> None:
        """导入 conftest 后，PYTEST_DEBUG_TEMPROOT 应指向一个新建的独立目录。

        用一个干净的子进程验证，避免测试进程自身已被 pytest 初始化过。
        """
        env = {k: v for k, v in os.environ.items() if k != "PYTEST_DEBUG_TEMPROOT"}
        code = (
            "import os, sys\n"
            f"sys.path.insert(0, {str(TESTS_DIR)!r})\n"
            "import conftest  # noqa: F401\n"
            "print(os.environ.get('PYTEST_DEBUG_TEMPROOT', ''))\n"
        )
        proc = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True, env=env, timeout=120
        )
        assert proc.returncode == 0, proc.stderr
        temproot = proc.stdout.strip().splitlines()[-1]

        assert temproot, "conftest 没有设置 PYTEST_DEBUG_TEMPROOT"
        # 关键：不能落在系统 TMPDIR（沙箱下是 /var/folders/...）—— 那正是 EEXIST 的来源
        assert temproot.startswith("/tmp/"), f"临时根没有隔离到 /tmp: {temproot}"
        assert Path(temproot).is_dir(), f"临时根不存在: {temproot}"

    def test_explicit_basetemp_is_respected(self) -> None:
        """显式传 --basetemp 时不干预调用方的选择。"""
        env = {k: v for k, v in os.environ.items() if k != "PYTEST_DEBUG_TEMPROOT"}
        code = (
            "import os, sys\n"
            f"sys.path.insert(0, {str(TESTS_DIR)!r})\n"
            "import conftest  # noqa: F401\n"
            "print(os.environ.get('PYTEST_DEBUG_TEMPROOT', 'UNSET'))\n"
        )
        proc = subprocess.run(
            [sys.executable, "-c", code, "--basetemp=/tmp/explicit"],
            capture_output=True,
            text=True,
            env=env,
            timeout=120,
        )
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.strip().splitlines()[-1] == "UNSET"


# ---------------------------------------------------------------------------
# 二、测试库端口探测
# ---------------------------------------------------------------------------


class TestDatabaseUrlResolution:
    def test_with_port_keeps_credentials_and_dbname(self, conftest_mod) -> None:
        url = conftest_mod._with_port(
            "postgresql+asyncpg://u:p@db.internal:15432/ekb_test", 55432
        )
        assert url == "postgresql+asyncpg://u:p@db.internal:55432/ekb_test"

    def test_with_dbname_forces_ekb_test(self, conftest_mod) -> None:
        url = conftest_mod._with_dbname("postgresql+asyncpg://ekb:ekb@h:5432/ekb", "ekb_test")
        assert url.endswith("/ekb_test")

    def test_falls_back_to_candidate_when_configured_port_dead(
        self, conftest_mod, monkeypatch
    ) -> None:
        """配置端口不可达时，自动切到存活的项目端口并打印修正提示。"""
        monkeypatch.setattr(
            conftest_mod,
            "_db_url_from_env_file",
            lambda: "postgresql+asyncpg://ekb:ekb@127.0.0.1:15432/ekb",
        )
        monkeypatch.setattr(
            conftest_mod,
            "_tcp_alive",
            lambda url, timeout=0.4: url.rpartition(":")[2].split("/")[0] == str(
                conftest_mod._DB_PORT_CANDIDATES[0]
            ),
        )
        resolved = conftest_mod._resolve_test_db_url()
        assert resolved.endswith("/ekb_test")
        assert f":{conftest_mod._DB_PORT_CANDIDATES[0]}/" in resolved
        assert "55432" in resolved

    def test_candidate_order_prefers_manual_container_port(self, conftest_mod) -> None:
        """候选顺序：手工容器 55432 优先于 compose 设计值 15432。

        先探 55432 是因为它是「本机当前实际在跑」的那一个；15432 只在
        compose 起来后才存在。
        """
        assert conftest_mod._DB_PORT_CANDIDATES[0] == 55432
        assert conftest_mod._DB_PORT_CANDIDATES[1] == 15432

    def test_configured_url_is_used_when_alive(self, conftest_mod, monkeypatch) -> None:
        """配置端口存活时不做任何改写。"""
        monkeypatch.setattr(
            conftest_mod,
            "_db_url_from_env_file",
            lambda: "postgresql+asyncpg://ekb:ekb@127.0.0.1:15432/ekb",
        )
        monkeypatch.setattr(conftest_mod, "_tcp_alive", lambda url, timeout=0.4: True)
        resolved = conftest_mod._resolve_test_db_url()
        assert resolved == "postgresql+asyncpg://ekb:ekb@127.0.0.1:15432/ekb_test"


# ---------------------------------------------------------------------------
# 三、自检脚本
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def dev_env_check_mod():
    return _load("ekb_dev_env_check", BACKEND_DIR / "scripts" / "dev_env_check.py")


class TestDevEnvCheckScript:
    def test_mask_hides_password(self, dev_env_check_mod) -> None:
        masked = dev_env_check_mod._mask(
            "postgresql+asyncpg://ekb:supersecret@127.0.0.1:55432/ekb"
        )
        assert "supersecret" not in masked
        assert "ekb:***@127.0.0.1:55432" in masked

    def test_known_ports_cover_both_startup_paths(self, dev_env_check_mod) -> None:
        assert 55432 in dev_env_check_mod.KNOWN_PORTS
        assert 15432 in dev_env_check_mod.KNOWN_PORTS

    def test_script_exits_cleanly(self) -> None:
        """脚本本身要能跑通：退出码只能是 0（连上）或 2（连不上并给指引）。"""
        proc = subprocess.run(
            [sys.executable, "scripts/dev_env_check.py"],
            cwd=str(BACKEND_DIR),
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert proc.returncode in (0, 2), proc.stdout + proc.stderr
        assert "Traceback" not in proc.stderr, proc.stderr
