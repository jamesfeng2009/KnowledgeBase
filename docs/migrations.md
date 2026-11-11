# 数据库迁移规约（必读）

> 一句话：**本仓的迁移必须手写。`alembic revision --autogenerate` 只能用来"看差异"，不能用来"生成要 apply 的迁移"。**

## 为什么

本仓 schema 由 100+ 个**手写**迁移建立，迁移里显式命名了约束（`fk_documents_tenant_id`）、
建了模型从未声明的索引（`ix_constraint_rules_domains` 等）、并给列与表加了 comment。
ORM 模型是后来逐步补的查询映射，两边从未对齐。

在"只跑过迁移"的库上跑一次 autogenerate，历史峰值曾达到：

| 差异类型 | 数量 | 如果 apply |
| --- | --- | --- |
| `remove_index` | 84 | 生产索引被删，查询计划雪崩 |
| `remove_fk` | 33 | 外键消失，脏数据开始堆积 |
| `modify_comment` / `modify_default` | 184 / 139 | 迁移文件被 COMMENT ON 噪音淹没 |

## 两道防线

### 1. `include_object` 过滤器（`backend/alembic_drift.py`）

`alembic/env.py` 把它传给 `context.configure(include_object=...)`：

- **库里有、模型没有**的索引 / 外键 / 唯一约束 / CHECK 约束 → 对 autogenerate **隐身**，
  不会被提议删除。即"迁移是 schema 权威，模型不是"。
- **模型侧新增**的对象照常报告（新加索引/外键不会漏）。
- 表与列不做过滤（删表删列属破坏性变更，交给门禁硬失败）。

要下线一个索引/外键：**手写迁移** + 显式 `op.drop_index(...)` / `op.drop_constraint(...)`，
再同步删掉模型上的声明。这是刻意的摩擦。

### 2. 漂移门禁（`backend/scripts/check_migration_drift.py`）

```bash
cd backend
DATABASE_URL=postgresql+asyncpg://ekb:ekb@localhost:5432/ekb \
  python scripts/check_migration_drift.py
```

- 破坏性（`remove_table` / `remove_column` / `remove_index` / `remove_fk` / `remove_constraint`）
  与结构性（`add_table` / `add_column` / `add_index` / `add_fk` / `modify_type` / `modify_nullable`）
  退出码 **1**；
- 注释与默认值差异只告警，退出码 **0**；
- 未知操作类型归入"结构性"（宁可多拦，不可漏放）。

CI 挂在 `.github/workflows/backend-migrations.yml` 的 `Autogenerate drift gate` 步骤：
先 `alembic upgrade head` 建一个干净库，再跑门禁。**红灯意味着有人正在让模型偏离真实 schema。**

## 写一个迁移的正确姿势

1. **先想清楚 DDL**，手写 `backend/alembic/versions/` 下的文件（命名沿用
   `YYYY_MM_DD_HHMM-<rev>_<slug>.py`），`upgrade()` / `downgrade()` 必须**成对可逆**；
2. 同步更新 `backend/app/models/` 里的模型声明（列、类型、`BigInteger` 别写成 `Integer`）；
3. 本地跑：

   ```bash
   python -m alembic upgrade head          # 新建的空库上必须一次跑通
   python -m alembic downgrade -1          # 回滚必须成功
   python -m alembic upgrade head          # 再升回来
   python scripts/check_migration_drift.py # 破坏性/结构性漂移必须为 0
   ```

4. 提交前跑 `pytest tests/test_migrations_schema_parity.py tests/test_migration_drift_guard.py`。

## 两个容易踩的坑

1. **表定义不在 `app/models/` 包里**：`eval_results`（`app/eval/repository.py`）、
   `tool_audit_log`（`app/models/tool_audit.py` 未在 `__init__` 导入）曾因此被 autogenerate
   判成"多余的表"→ 生成 `DROP TABLE`。`env.py` 现在显式导入这两个模块，
   `tests/test_migrations_schema_parity.py` 守着这条线。
2. **测试走 `create_all`，生产走迁移**：`tests/conftest.py` 用 `Base.metadata.create_all`
   建测试库，所以"迁移漏了某张表"在单测里查不出来，只有上面那个 parity 测试能抓到。
   任何新表都必须同时有：模型 + 迁移 + parity 测试覆盖。
