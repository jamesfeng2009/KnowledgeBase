# 种子文档灌库与运行层验证（seed_kb.py）

> 来源：Easy Data x AI Task 2 / P1「数据在哪」缺口 T1
> 脚本：`backend/scripts/seed_kb.py`　单测：`backend/tests/test_seed_kb.py`

## 解决什么问题

`finetune/seed_documents.py` 的 40 篇样本文档、`test_documents/` 的 101 份真实文件，
此前只有「写进 Document 表」的路径（`seed_documents.run_seed`），**不解析、不切片、
不建向量索引**，因此检索层召回不到——这就是 P1 十问「代码层有数据 ≠ 运行层有数据」
无法闭合的根因。

`seed_kb.py` 补上一条完整、可重复执行的路径：

```
收集文档 → 确定性 doc_id → 写库(flush) → 统一 commit
         → 逐篇跑 _process_document_async（解析→分块→向量→索引→发布）
         → 可选：跑 P1 十问运行层评测，输出 N/10
```

## 用法

在 `backend/` 目录下执行：

```bash
# 灌 40 篇内置样本文档（默认建/取「Task2 种子知识库」）
.venv/bin/python scripts/seed_kb.py --source seed

# 灌 test_documents/ 下全部真实文件（md/docx/pdf/pptx/xls/xlsx）
.venv/bin/python scripts/seed_kb.py --source test_documents

# 灌全部 + 跑 P1 十问评测
.venv/bin/python scripts/seed_kb.py --source all --verify

# 指定租户/知识库，只灌前 5 篇，JSON 输出（便于 CI 归档）
.venv/bin/python scripts/seed_kb.py --source seed --tenant <uuid> --kb-name 我的库 --count 5 --json
```

参数：

| 参数 | 说明 |
|---|---|
| `--source seed\|test_documents\|all` | 灌库来源，默认 `seed` |
| `--tenant <uuid>` | 目标租户；不传则建/取默认种子租户（**注意：不传每次会新建租户**） |
| `--kb-name <name>` | 目标知识库名称，默认 `Task2 种子知识库` |
| `--count N` | 只灌前 N 篇 |
| `--verify` | 灌库后跑 P1 十问检索评测，输出 N/10 |
| `--strict` | 配合 `--verify`，未全命中时退出码 2 |
| `--json` | JSON 输出报告 |

退出码：`0` 全部成功；`1` 有失败或依赖错误；`2`（`--strict` 下）评测未全命中。

## 幂等保证

- `doc_id = uuid5(namespace, source|tenant|kb|key)` —— 同一租户+知识库+文档，ID 恒定。
- 灌库前比对 `content_hash`（SHA-256 正文），内容未变且已 `published` → 跳过，
  不重复写库、不重复建索引。
- 实测：同租户重跑 3 篇 → `skipped_unchanged=3, created=0, indexed=0`。

## 两阶段设计（关键）

流水线 `_process_document_async` 用**独立数据库 session** 重新加载文档。若写库后
不 commit，它会报「文档不存在」。因此脚本严格分两阶段：

1. 阶段 1：所有文档 upsert 到 DB，`session.flush()` 后统一 `session.commit()`；
2. 阶段 2：逐篇调用 `_process_document_async` 建索引。

这与项目既有样板 `eval_resume_seed.py` 一致。

## 运行层验证的环境前提

`--verify` 的检索指标依赖：

- 向量库：`OPENSEARCH_URL`（默认 `http://localhost:9200`）或 Milvus/Qdrant；
- embedding：`DEPLOY_MODE` 对应的 DashScope / TEI 服务。

这些服务**未启动时**，灌库仍能完成入库（DB 层成功），但检索召回为空，评测会
判为未命中。脚本会打印 `[VERIFY][SKIP]` 及每问的命中/错误明细，不把环境缺失
伪装成「数据不存在」。当前本机状态（2026-11-20）：PostgreSQL 通、DashScope Key
已在 `.env.local`，但 OpenSearch(502) / Milvus(closed) 未启动，故 N/10 未实测。

## P1 十问

脚本内置 `P1_QUESTIONS`（来自交付物 A），每问带期望要点 `points`。命中判定用
检索正文对要点的关键词覆盖率（≥0.5 判命中），不依赖 `sample.jsonl` 里的占位符
`doc_id`（`doc_reimbursement_01` 这类 ID 与真实 UUID 不一致，不能直接比对）。

## 单测

```bash
.venv/bin/python -m pytest tests/test_seed_kb.py -q
```

8 个用例覆盖：确定性 ID、40 篇种子文档、101 份测试文件枚举、命中判定、报告结构、
幂等跳过（内容未变 → 不建索引）、变更重建（内容变化 → 更新 + 建索引）。
全部离线可跑，不依赖 DB / 向量库。
