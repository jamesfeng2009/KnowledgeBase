# 缺陷跟踪任务清单 — 记忆模块 & PDF 解析审计

> 来源：2026-11-18 全项目代码审计（双研究代理深扫 + P0 逐条人工复核，pymupdf 行为为本机 venv 实测）。
> 状态标记：`[ ]` 未开始 / `[x]` 已完成（完成必须满足验收标准，DoD＝测试通过＋闸门先打红一次）。
> 证据标注：★＝已人工复核代码/实测；其余为代理取证（行号以 2026-11-18 HEAD 8f19bf0 为准，修复前先核对行号是否漂移）。
> 修复顺序建议：A1→A2→A3（修完用真实 PDF 端到端跑通）→ B1→B2 → 其余按优先级。
> 共性根因：测试 mock 掉真实边界（fitz 全 mock、create_all 不建 RLS、chord 入口无真实调用），全绿但生产断链。每修一条 P0 必须补一条能打到真实边界的测试。

---

## A. PDF 解析链路

### P0（三个叠加 → 生产上传 PDF 的增强解析实际从未生效）

- [x] **A1. chord 主管线 NameError：`tenant_id` 未定义** ★ ✅ 2026-11-18
  - 完成证据：`_parse_and_chunk_async(doc_id, tenant_id=None)` 签名 + 调用方传参（document_tasks.py:587/689）；缺参时 `resolve_task_tenant` 按 doc_id 反查。闸门 `test_pdf_pipeline_hardening.py::TestA1A2PipelineIntegration` 真实调用（显式租户 + 反查两条）；变异（删 tenant_id 参数）→ 1 failed，恢复 → 1 passed。
  - 证据：`backend/tasks/document_tasks.py:709` 引用自由变量 `tenant_id`；函数签名 `:684` 仅有 `doc_id`；调用方 `:584` 未传参；全文件无模块级同名变量。
  - 后果：chord 主路径解析必炸 → 重试 → 死信。
  - 修复点：`_parse_and_chunk_async` 增加 `tenant_id` 参数并由调用方传入（或统一走 `resolve_task_tenant` 的 doc_id 反查路径）。
  - 验收：真实调用 `_parse_and_chunk_async` 的集成测试（现有 test_document_tasks_chunker.py:787 仅 docstring 提及，无真实调用）；先构造缺参场景打红。

- [x] **A2. minio:// 路径直传解析器，无下载步骤** ★ ✅ 2026-11-18
  - 完成证据：新增 `_localized_file_path` 上下文管理器（minio:// → download_file_to_path 临时文件，finally 恢复原路径+删临时文件）；两条管线解析失败/空内容早退标 failed + warning，返回值保留 warnings/parse_status/char_count 契约。闸门：minio 下载端到端 + 空内容反向闸门；变异×2（禁下载分支 / 禁空内容早退）各 → 1 failed，恢复 → passed。
  - 证据：上传后 `file_path="minio://…"`（`backend/app/api/v1/documents.py:380-392`）；`backend/tasks/document_tasks.py:1481/1561/1572` 直接交给 Docling/fitz；全文件 grep 无 `download_file_to_path`/minio 下载逻辑（对比 `video_tasks.py:147` 有下载）。
  - 后果：上传 PDF 实际从未被解析；失败被吞返回 `doc.content_text or ""`，空内容仍标 `parse_status="parsed"`（`:779`）。
  - 修复点：解析前统一「minio:// → 临时本地文件」下载步骤；解析失败/空内容不得标 parsed（应 failed/partial 并带 warning）。
  - 验收：MinIO 上传真实 PDF → 解析出非空文本的端到端测试；空内容标 parsed 的路径要有反向闸门（断言 parse_status != "parsed"）。

- [x] **A3. PDFParser 在 doc.close() 后取 len(doc)，100% 抛异常** ★ ✅ 2026-11-18
  - 完成证据：close 前缓存 `page_count = len(doc)`（pdf_parser.py:119），日志用缓存值。闸门 `TestA3RealPdfParse` 用真实 fitz PDF（非 mock）；变异（日志改回 len(doc)）→ 2 failed，恢复 → 2 passed。
  - 证据：`backend/app/document/pdf_parser.py:145` close，`:150` `pages=len(doc) if doc else 0`；本机实测 pymupdf 1.26.7 对 closed doc 取 bool/len 抛 `ValueError: document closed`。
  - 后果：每次增强解析在最后日志步炸掉，被 `document_tasks.py:1565` 吞掉降级纯文本，表格/OCR/图片全丢。测试用 MagicMock 掩盖（test_document_parser.py:239-241）。
  - 修复点：close 前缓存 `page_count = len(doc)`，日志用缓存值。
  - 验收：用真实 fitz 文档（非 mock）跑 `PDFParser.parse` 的测试；先打红（当前必抛）。

### P1

- [x] **A4. 加密 PDF 无处理，静默变空文档** ✅ 2026-11-18
  - 完成证据：`EncryptedPDFError`（needs_pass 且空密码失败 → 显式 raise），管线捕获后标 failed + 「加密」warning；fitz 兜底同样检查 needs_pass。闸门 `TestA4EncryptedPdf`（真实 AES-256 加密样例，解析器级 + 管线级）；变异（删 raise）→ 1 failed，恢复 → 2 passed。
  - 证据：全链路无 `needs_pass`/`authenticate`；加密页 get_text 抛 ValueError，经 `pdf_parser.py:192` 传播、`document_tasks.py:1581` 兜底返回 ""。
  - 修复点：检测 `needs_pass` → 显式标 failed + 用户可见 warning「PDF 已加密」，不得静默空。
  - 验收：加密样例 PDF 测试，断言状态与 warning。

- [x] **A5. 栏检测逻辑缺陷，阅读顺序错乱** ✅ 2026-11-18
  - 完成证据：`_detect_columns` 末尾新增缩进伪栏合并 pass（左栏右缘取 x1 中位数，gap < min_gap 且按行高容差垂直重叠 → 归位同栏）。闸门 `TestA5ColumnDetection` 3 条（合成缩进块 + 真实单栏缩进 PDF + 真实双栏 PDF）；变异（禁合并 pass）→ 2 failed / 1 passed（双栏守卫为正向证人），恢复 → 3 passed。
  - 证据：`backend/app/document/layout_analyzer.py:131` `min_gap` 算而未用；`:98` `_vertically_overlap` 死代码；实际仅按 x0 容差 20pt 聚栏（`:103-105、139-151`）→ 单栏页缩进块（引用/列表）被误判新栏移至页尾。
  - 修复点：启用 min_gap 判据或重写聚栏；补单栏缩进、双栏、三栏样例断言阅读顺序。
  - 验收：构造含缩进块的单栏 PDF，断言文本顺序与原文一致。

- [x] **A6. chunker 丢内容 / 切坏表格** ✅ 2026-11-18
  - 完成证据：MD/HTML 结构化分块保留首标题前导内容；`_split_by_tokens` 改表格感知编排（`_TABLE_SEGMENT_RE` 分段，表格原子保留，超长走 `_split_table_by_rows` 按行切 + 重复表头 + 闭合标签）。闸门 `TestA6Chunker` 4 条；变异×2（丢前导 / 禁表格分段）→ 2 failed、1 failed，恢复 → 4 passed（小表格原子性为正向证人）。
  - 证据：`backend/app/rag/chunker.py:731-733`（HTML）、`:652-654`（MD）丢弃首个标题前的全部内容；`:1043-1045` 硬切可把 `<table>` 从中间切断。
  - 修复点：首标题前内容保留为独立块；表格作为原子块不切分（超长表格按行切并保留表头）。
  - 验收：无标题前导文本、超 chunk 上限表格两个样例的断言测试。

- [x] **A7. 图片命名空间冲突：全部写入 unknown/ 互相覆盖** ✅ 2026-11-18
  - 完成证据：`PDFParser.parse(file_path, doc_id=None)` 显式参数贯穿 `_extract_images` → `upload_image(doc_id=…)`，删除 `getattr(page,"_doc_id")` 假接口；管线传真实 doc.id。闸门 `TestA7ImageNamespace` 2 条（双文档 key 不冲突 + 管线传真实 doc_id）；变异（写死 "unknown"）→ 2 failed，恢复 → 2 passed。
  - 证据：`backend/app/document/pdf_parser.py:359` `doc_id=getattr(page,"_doc_id","unknown")`，全仓库无人设置 `_doc_id`；`image_storage.py:246` 按 `doc_id/pageN_imgM` 存储。
  - 修复点：parse 入口显式传 doc_id 贯穿到图片提取；删除 `_doc_id` getattr 假接口。
  - 验收：两个文档各含同名页图片，断言存储 key 不冲突。

- [x] **A8. 混合扫描页不触发 OCR + 超限静默丢弃** ✅ 2026-11-18
  - 完成证据：新配置 `PDF_SCAN_OCR_MIN_TEXT_CHARS=32`，文本量低于阈值即判扫描主导页触发 OCR；超限页记 `last_parse_warnings` 并经管线写入 doc.parse_warnings。闸门 `TestA8ScanOcr` 3 条（混合页 / 21 页超限 warning / 管线传播）；变异×2（阈值退回整页空 / 删 warning）→ 1 failed、2 failed，恢复 → 3 passed。
  - 证据：`pdf_parser.py:108-112` 仅整页无文本才 OCR；超 `scan_ocr_max_pages`(20) 的扫描页静默丢弃。
  - 修复点：按文本覆盖率阈值判定混合页；超限页记 parse_warning 而非静默。
  - 验收：混合页样例 + 21 页扫描 PDF 的 warning 断言。

- [x] **A9. ai_eval 路由内同步 ML 解析阻塞事件循环** ✅ 2026-11-18
  - 完成证据：`docling_parser.py` parse 与 _parse_raw 两处同步 convert 均包 `asyncio.to_thread`（:125/:166）。闸门 `TestA9DoclingThread` 2 条（FakeConverter 断言线程 ID ≠ 事件循环线程）；变异（去 to_thread，2 处）→ 2 failed，恢复 → 2 passed。
  - 证据：`backend/app/api/v1/ai_eval.py:811` 请求内 await → `services/ai_eval/doc_parse_service.py:305/395` → `document/docling_parser.py:123` 同步 convert，无 to_thread。
  - 修复点：同步 convert 包 `asyncio.to_thread`（或移入任务队列）。
  - 验收：并发请求下事件循环延迟测试或代码审查确认无同步阻塞调用。

### P2

- [ ] **A10. 页眉页脚去除不存在**
  - 证据：layout_analyzer 只排序不剔除，每页页眉页脚混入正文。
  - 修复点：跨页重复块检测（同位置同文本 ≥N 页）剔除。
- [ ] **A11. 图片链路断裂（minio:// src 前端不可用 + legacy 图片不入跨模态索引）**
  - 证据：上传默认关（`config.py:255`）；`<img src="minio://…">` 全仓库无 presigned/HTTP 转换；跨模态提取仅 Docling 路径（`document_tasks.py:1408-1416`），且 `:1412` `_parse_raw` 对 PDF 二次全量解析。
  - 修复点：输出前统一 minio:// → presigned URL；legacy 路径图片接入跨模态索引；消除二次解析。
- [ ] **A12. 页码丢失、页数失真**
  - 证据：`PAGE_SEPARATOR` 默认 ""（`config.py:290`），ParsedSection.page 被丢弃；页数按 `<h2>` 数推断（`document_tasks.py:294-296`）。
  - 修复点：解析结果携带真实页数元数据，落库 page_count 用真实值。
- [ ] **A13. 表格文本重复入库**
  - 证据：get_text 已含表格文字，find_tables 再追加 HTML（`pdf_parser.py:107-129`），无 bbox 去重。
  - 验证方法：解析含表 PDF 对比字符数；修复按 bbox 从正文剔除表格区域文本。
- [ ] **A14. 重索引不重解析，文件更新后用旧文本**
  - 证据：`document_tasks.py:1332` content_text 非空即返回。
  - 验证方法：换文件后 reindex，对比 content_hash；修复按 content_hash 判定是否重解析。
- [ ] **A15. sniff 拒绝带前导垃圾的合法 PDF**
  - 证据：`backend/app/document/sniff.py:78` 要求 `%PDF-` 在偏移 0。
  - 修复点：在前 N 字节（如 1024）内搜索魔数。
- [ ] **A16. 降级路径 fitz 异常不 close，句柄泄漏**
  - 证据：`document_tasks.py:1572-1576`。
  - 修复点：try/finally 或上下文管理器。
  - 备注（2026-11-18）：句柄泄漏已随 A4 的 fitz 兜底重写一并修复（pdf_doc=None + finally close），未做独立验收，待确认后勾选。
- [ ] **A17. PDF 测试盲区：全 mock，真实样例零引用**
  - 证据：PDF 测试全部 mock fitz；`test_documents/pdf/` 20 个真实样例无任何测试引用。
  - 修复点：新增真实样例端到端解析测试（至少覆盖：普通文本、表格、扫描页、多栏、加密），作为 A1-A3 的联合验收闸门。
  - 备注（2026-11-18）：`tests/test_pdf_pipeline_hardening.py`（23 条）已用真实 fitz 生成样例覆盖文本/多栏/扫描/加密/图片并真实调用管线；`test_documents/pdf/` 20 个存量样例仍无测试引用，未完成。

---

## B. 记忆模块

### P0

- [ ] **B1. 记忆写入不填 tenant_id，fail-closed RLS 下写入被拒/读取恒空** ★
  - 证据：`backend/app/memory/mem0_manager.py:171-184` MemoryFact 构造无 tenant_id；`graphiti_manager.py:56-62` register_entity 同；RLS 迁移 e8f9a0b1c2d3:56-59 要求 `tenant_id::text=GUC` 且 FORCE，生产角色 ekb_app 无 BYPASSRLS；失败仅记 warning 静默。对比正确写法：`conversation_repository.py:127-131`。
  - 后果：生产多租户下记忆功能整体失效且无感知。测试用 create_all 不建 RLS（conftest.py:203-204）故全绿。
  - 修复点：add_fact/register_entity 及所有记忆写入路径显式填 tenant_id（从会话/上下文解析）；写入失败不得静默。
  - 验收：带真实 RLS 的集成测试（非 create_all），断言租户 A 写入后租户 B 读不到、租户 A 能读到；先打红。

- [ ] **B2. 向量维度错位残余：saas 模式 3072 维 vs 列宽 1024** ★
  - 证据：`backend/app/models/memory.py:62` `Vector(settings.DASHSCOPE_EMBED_DIM)`（默认 1024，config.py:182）；默认 `DEPLOY_MODE="saas"`（config.py:128）→ OpenAIEmbedder dim=3072（embedder.py:48、182-186 注册表）。上次修复（ba9dd63）只对齐 DashScope 路径。
  - 后果：saas 模式 `flush` 报 dimension mismatch 且不在 try 内（mem0_manager.py:186）；查询侧 pgvector 报错降级 JSONB 后 `zip` 截断**静默算错相似度**（mem0_manager.py:38）。
  - 修复点：记忆向量列宽随 DEPLOY_MODE 对应 embedder.dim（迁移或按模式建列）；降级 zip 前断言维度一致，不一致显式报错不得截断。
  - 验收：跨部署模式参数化测试（saas/saas_dashscope/private_*），断言 embedder.dim == 列宽；维度不一致降级路径打红测试。

### P1

- [ ] **B3. 单租户（tenant_id=NULL）异步记忆全丢**
  - 证据：`backend/tasks/memory_tasks.py:57-87` resolve_tenant_of_row 返回 None → fail-closed 查不到会话 → 按「会话已删除」跳过写入；默认开关开启（config.py:846）。
  - 修复点：区分「会话不存在」与「单租户 NULL」两种情形，后者正常写入。
  - 验收：tenant_id=NULL 会话的记忆写入集成测试。

- [ ] **B4. checkpoint 每轮被存根覆写，L2 恢复形同虚设**
  - 证据：`backend/app/services/chat_service.py:760`、`memory_tasks.py:101` 每轮写 `{"iteration":0}`，冲掉 `agents/base.py:335-345` 的真实状态；加载仅渲染提示语（memory_manager.py:115-119），无真状态恢复。
  - 修复点：chat 路径不得覆写 agent checkpoint；恢复路径真正还原状态或明确降级为「仅摘要」并改名去误导。
  - 验收：agent 写入状态 → chat 一轮 → 断言状态未被冲掉。

- [ ] **B5. event_log.append 吞错污染共享事务 + seq 竞态丢事件**
  - 证据：`backend/app/memory/event_log.py:202-210` 失败仅返回 0，无 savepoint，共享 session 事务 abort 后续操作必挂；`:135-139` seq 竞态 IntegrityError 即丢事件，注释称「可重试」但无重试实现。
  - 修复点：append 用嵌套事务（begin_nested）隔离失败；seq 冲突实现有限重试。
  - 验收：append 失败后同 session 后续操作成功的测试；并发 append 不丢事件的测试。

- [ ] **B6. agent_checkpoints / agent_event_logs 无 tenant 列无 RLS**
  - 证据：`backend/app/models/checkpoint.py:29-51`；隔离全靠上游归属校验（chat_service.py:186-188）。
  - 验证方法：跨租户 session_id 直调 load_checkpoint 是否越权。
  - 修复点：补 tenant 列 + RLS（与 B1 同一迁移批次）。

### P2（断链/未接线功能）

- [ ] **B7. 混合恢复未接线：event_log 生产零消费**
  - 证据：`save/load_checkpoint_with_event_log` 仅测试调用；`agent_event_logs` 恒空；消费方 `delivery_chain.py:226` 查空表。
  - 修复点：主链路接线或显式下线该能力（二选一，不留僵尸代码）。
- [ ] **B8. build_context 忽略 recent_messages，ctx.short_term 恒空**
  - 证据：`memory_manager.py:205-322`；历史实际靠 Redis 兜底（chat_service.py:1075-1103）；`SHORT_TERM_WINDOW_SIZE:33` 死代码。
  - 修复点：接线或删除死参数，与 Redis 兜底职责划清。
- [ ] **B9. 用户纠错/偏好管理无生产入口**
  - 证据：`correct_fact`/`set_preference`/`clear_working_memory`/`Mem0Manager.cleanup_expired`/`sync_to_graph` 无生产调用方；Graphiti 偏好时间线永不写入。
  - 修复点：补 API 入口或下线；cleanup_expired 与定时任务（scheduled_tasks.py:548-570）二选一统一。
- [ ] **B10. private_finetuned 模式未注册 embedder，记忆退化纯关键词**
  - 证据：`embedder.py:182-198` 注册表无 private_finetuned（有日志，降级合理但属缺口）。
  - 修复点：注册微调 embedder 或文档显式声明该模式记忆降级。

### 疑似问题（先验证再定性，验证通过转正式任务）

- [ ] **B11. get_preference 并发同键写可致 MultipleResultsFound**
  - 证据：`mem0_manager.py:625`。验证：并发 set_preference 压测。
- [ ] **B12. save_milestone 读-改-写非原子，并发丢里程碑**
  - 证据：`backend/app/memory/checkpoint.py:350-356`。验证：并发重试场景测试。
- [ ] **B13. Redis 热层条目全损坏时返回 [] 而非 None，跳过 PG 回填**
  - 证据：`short_term_cache.py:126-129` + `chat_service.py:1083-1087`。验证：注入全损坏缓存数据。
- [ ] **B14. 定时清理绕过 cleanup_expired，漏 NULL 租户行且不写 forgotten_reason**
  - 证据：`scheduled_tasks.py:548-570`。验证：对比两条清理路径行为差异。

---

## C. 测试体系补强（伴随上述修复，不单独延后）

- [ ] **C1. RLS 集成测试基座**：现有 conftest create_all 不建 RLS（conftest.py:203-204），B1/B6 的验收依赖一个能真实执行 RLS 策略的测试 fixture。
- [ ] **C2. 真实 PDF 端到端闸门**：即 A17，A1-A3 修完必须联合跑通。（2026-11-18：A1-A9 闸门已用真实 fitz 样例联合跑通 23 条，存量 20 样例接入未完成）
- [ ] **C3. chord 入口真实调用测试**：即 A1 验收，杜绝「函数从未被真实调用」类缺陷再次全绿存活。（2026-11-18：`_parse_and_chunk_async` 已有真实调用集成测试，process_document 任务级入口仍走 mock）

---

## 范围外勘误（2026-11-18，A1–A9 全量回归时发现，未修复）

全量 `pytest tests`：56 failed / 5334 passed。用 HEAD（8f19bf0）干净 worktree 复跑同批失败定性：
**52 条为 HEAD 既有失败（与本次改动无关），4 条为本次引入回归（已修复，见下）**。

既有 52 条按文件分布（均在 HEAD 复现）：
| 文件 | 条数 | 备注 |
|------|------|------|
| tests/test_dashscope_provider.py | 13 | DashScope provider/embedder/factory 全线 |
| tests/test_provider_circuit_breaker.py | 13 | 熔断器相关 |
| tests/test_llm_stream_abort.py | 8 | 流中止/半开探针 |
| tests/test_provider_pool.py | 6 | provider 池工厂 |
| tests/test_p2_model_selection.py | 5 | 按模型选择 provider/engine |
| tests/test_b3_dashscope_feishu.py | 3 | 已确认 httpx URL 解析 ValueError（':1]'） |
| tests/test_fault_injection.py | 3 | embedding 超时熔断 |
| tests/test_fact_extraction.py | 1 | 事实去重 |

疑似共性根因（未验证，仅线索）：provider 构造/endpoint 解析类似（dashscope endpoint 配置或 httpx 版本行为），建议单独立项排查。

本次引入又修复的 4 条回归：
- test_task_lock.py::TestProcessDocumentLockCoverage ×2 —— 测试 fake `_fake_parse(doc_id)` 未接受 A1 新增 tenant_id 关键字，更新 fake 签名（锁行为本身未破坏）。
- test_upload_summary.py::TestDocumentTaskWarnings ×2 —— A2 失败早退返回值丢了 warnings/parse_status/char_count 字段（下游 eval_resume_seed.py:175 与摘要响应依赖），已在两处早退 dict 补齐字段。

---

## 已完成 / 明确不做

- A1–A9（2026-11-18）：PDF 解析链路 9 条修复完成。闸门 `backend/tests/test_pdf_pipeline_hardening.py` 23 条（红基线 21 failed/2 passed → 修后 23 passed）；12 次串行变异自证全部「打红→恢复→绿」；受影响套件合跑全绿；全量回归仅剩 52 条 HEAD 既有失败（见上方勘误）。
- 不算缺陷：CRM/ERP/邮件/OA connector 与开放平台多媒体接口为显式 501/ConnectorNotImplementedError，属已知未实现、行为诚实，不在本清单范围。
- 好消息备查：conflict_arbiter 与 forgetting 已真实接线（memory_manager.py:707、mem0_manager.py:93）；mem0/graphiti 为自研 PG 实现，无外部依赖坑。