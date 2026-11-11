# 本地开发环境 — 两个必踩的坑与自检方式

> 这两条都在 2026-11-10 修掉了，并各自加了回归测试。这里记录「为什么」，因为
> 它们的症状完全不像环境问题：一个是两百多个连接错误，一个是近百个
> `FileExistsError`，两次都先被误判成了代码 bug。

## 1. PostgreSQL 端口：`.env` 写的 ≠ 机器上在跑的

本仓有两套启动方式，宿主端口不同：

| 启动方式 | 宿主端口 | 说明 |
|---|---|---|
| `docker compose -f docker-compose.yml -f docker-compose.override.yml up -d postgres` | **15432** | `override` 里 `!override` 重映射（本机 5432 被其它项目的 PG 占着） |
| `docker run -d --name ekb-pg -p 55432:5432 ...` | **55432** | 手工容器；当前机器上实际在跑的就是这个，`ekb` / `ekb_test` 都在里面 |

`.env` 只能写一个。写错时 `uvicorn` 只报一条 Connection refused，跑测试则一次性
两百多个 ERROR，报错里**完全不提端口**。

**现在怎么处理**：`tests/conftest.py` 在导入期做一次 TCP 探测 —— 先试 `.env` 的
端口，不通就依次试 55432 / 15432，切换时在 stderr 打一行提示（含可直接粘贴的
`export DATABASE_URL=...`）。`uvicorn` 不做探测，所以 `.env` 仍然必须与实际
实例一致。

```bash
cd backend
.venv/bin/python scripts/dev_env_check.py     # 一眼看清连的是哪个库、什么角色
```

## 2. `--basetemp`：不加就冒出近百个 `FileExistsError`

症状：堆栈落在 `_pytest.tmpdir.TempPathFactory.getbasetemp` 的 `basetemp.mkdir()`，
每个用 `tmp_path` 的用例都 ERROR。根因是「环境 shim + 目录残留」叠加：

1. basetemp 默认取 `tempfile.gettempdir()/pytest-of-<user>`，上一轮跑完不会被清掉；
2. 该环境的 `sitecustomize` 把 `shutil.rmtree` 换成了「移入回收站」，pytest 建目录前
   的 `rm_rf()` 对这类路径静默失败 → 紧接着 `mkdir()` 撞上已存在目录 → EEXIST；
3. pytest 只对 `pytest-of-root` 重试一次（换成 `pytest-of-unknown`），那个目录同样
   存在，异常于是逃出到每个用例。

**现在怎么处理**：`tests/conftest.py` 导入期把 `PYTEST_DEBUG_TEMPROOT` 指向一次性的
`/tmp/ekb-pytest-root-*`，mkdir 走「不存在」分支。显式传 `--basetemp` 时不干预。

```bash
# 不用再加任何参数
.venv/bin/python -m pytest tests/ -q
```

## 3. 跑全量的推荐姿势

```bash
cd backend
.venv/bin/python -m pytest tests/ -q -p no:randomly
```

`-p no:randomly`：本仓测试对执行顺序敏感（部分文件会往 `sys.modules` 塞 mock），
随机顺序偶发假失败，排查时先关掉。

不要在中途编辑即将被收集的测试文件：pytest 收集阶段要 1–2 分钟，两次编辑之间
正好落在收集窗口会以「缺 import」的形式假失败。
