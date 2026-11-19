# 缺陷跟踪任务清单 — 记忆模块 & PDF 解析审计 & 解析策略对齐

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

- [x] **A10. 页眉页脚去除不存在**（2026-11-18 完成）
  - 证据：layout_analyzer 只排序不剔除，每页页眉页脚混入正文。
  - 修复点：跨页重复块检测（同位置同文本 ≥N 页）剔除。
  - 实测：`layout_analyzer.py` 新增 `header_footer_key`（顶/底 15% 带 + 位置量化 10pt）与 `detect_header_footer_keys`（每页去重计数 ≥min_pages）；`pdf_parser.py` 预检测 + `_extract_text_ordered` 剔除；配置 `PDF_HEADER_FOOTER_REMOVAL_ENABLED=True` / `PDF_HEADER_FOOTER_MIN_PAGES=3`。闸门 `test_pdf_p2_hardening.py::TestA10HeaderFooterRemoval` 5 条（含中部重复文本保留 / 2 页不启用 / 开关关闭 3 个正向证人）；变异（hf_keys 置空）红→恢复绿 PASS。
- [x] **A11. 图片链路断裂（minio:// src 前端不可用 + legacy 图片不入跨模态索引）**（2026-11-18 完成）
  - 证据：上传默认关（`config.py:255`）；`<img src="minio://…">` 全仓库无 presigned/HTTP 转换；跨模态提取仅 Docling 路径（`document_tasks.py:1408-1416`），且 `:1412` `_parse_raw` 对 PDF 二次全量解析。
  - 修复点：输出前统一 minio:// → presigned URL；legacy 路径图片接入跨模态索引；消除二次解析。
  - 实测：`minio_client.get_presigned_url`（boto3 签名，to_thread）；`image_storage.resolve_minio_image_urls`（去重签名、失败保留原址）+ `extract_pdf_images`（fitz 直提，小图过滤）；`documents.py:896-901` 详情端点输出前转换（存储层保留 minio://）；`_extract_images_for_cross_modal` PDF 分支改 fitz 直提，`_parse_raw` 不再被调。闸门 `TestA11ImagePipeline` 7 条（含签名失败保留原址、存储层不污染证人）；变异 3 次（提取置空 / 跳过签名 / 端点不转换）均红→恢复绿 PASS。
- [x] **A12. 页码丢失、页数失真**（2026-11-18 完成）
  - 证据：`PAGE_SEPARATOR` 默认 ""（`config.py:290`），ParsedSection.page 被丢弃；页数按 `<h2>` 数推断（`document_tasks.py:294-296`）。
  - 修复点：解析结果携带真实页数元数据，落库 page_count 用真实值。
  - 实测：`PDFParser.last_page_count`（close 前缓存）；`_parse_pdf` 增强/兜底两路均写真实页数；两条管线解析前清零、落库仅在无真实值时回退推断。闸门 `TestA12RealPageCount` 2 条（单测 + 真实管线落库 page_count==3）；变异（last_page_count 置 0）红→恢复绿 PASS。
- [x] **A13. 表格文本重复入库**（2026-11-18 完成）
  - 证据：get_text 已含表格文字，find_tables 再追加 HTML（`pdf_parser.py:107-129`），无 bbox 去重。
  - 验证方法：解析含表 PDF 对比字符数；修复按 bbox 从正文剔除表格区域文本。
  - 实测：`_extract_tables` 返回 (sections, bboxes)（len==4 守卫）；主循环先表格后正文，`_extract_text_ordered` 按 bbox 中心剔除；表格字符计入扫描判定防纯表格页误触 OCR。闸门 `TestA13TableTextDedup` 2 条（特征单元格全文恰好 1 次 + 表格外正文保留证人）；变异（exclude_bboxes=None）红→恢复绿 PASS。
- [x] **A14. 重索引不重解析，文件更新后用旧文本**（2026-11-18 完成）
  - 证据：`document_tasks.py:1332` content_text 非空即返回。
  - 验证方法：换文件后 reindex，对比 content_hash；修复按 content_hash 判定是否重解析。
  - 实测：Document 新增 `file_hash`（SHA-256，迁移 9f8e7d6c5b4a）；`_parse_document` 按源文件哈希判定——未变早退、变了重解析并更新哈希；视频/音频跳过哈希。闸门 `TestA14ReindexHash`（spy dispatch：首解析 1 次→未变不再调→换文件再调且哈希更新）；变异（哈希比较恒 True）红→恢复绿 PASS。
- [x] **A15. sniff 拒绝带前导垃圾的合法 PDF**（2026-11-18 完成）
  - 证据：`backend/app/document/sniff.py:78` 要求 `%PDF-` 在偏移 0。
  - 修复点：在前 N 字节（如 1024）内搜索魔数。
  - 实测：`sniff.py:80` 改 `b"%PDF-" in head`（head=前 1024 字节）；闸门 `test_sniff.py::test_pdf_tolerates_leading_garbage`（junk 前缀命中 / 超 1024 偏移不命中）；变异（回退 startswith）红→恢复绿 PASS。
- [x] **A16. 降级路径 fitz 异常不 close，句柄泄漏**（2026-11-18 独立验收完成）
  - 证据：`document_tasks.py:1572-1576`。
  - 修复点：try/finally 或上下文管理器。
  - 备注（2026-11-18）：句柄泄漏已随 A4 的 fitz 兜底重写一并修复（pdf_doc=None + finally close），未做独立验收，待确认后勾选。
  - 实测：独立闸门 `TestA16FitzFallbackClose`——强制兜底 + get_text 抛异常 + close 间谍，断言 close 被调且兜底确实进入（page_count==2 正向证人）；变异（finally 不 close）红→恢复绿 PASS。
- [x] **A17. PDF 测试盲区：全 mock，真实样例零引用**（2026-11-18 完成）
  - 证据：PDF 测试全部 mock fitz；`test_documents/pdf/` 20 个真实样例无任何测试引用。
  - 修复点：新增真实样例端到端解析测试（至少覆盖：普通文本、表格、扫描页、多栏、加密），作为 A1-A3 的联合验收闸门。
  - 备注（2026-11-18）：`tests/test_pdf_pipeline_hardening.py`（23 条）已用真实 fitz 生成样例覆盖文本/多栏/扫描/加密/图片并真实调用管线；`test_documents/pdf/` 20 个存量样例仍无测试引用，未完成。
  - 实测：`TestA17RealSamples` parametrize 20 个存量样例直跑 PDFParser——全部 4 页、正文 886~1217 字、零告警（断言 len>200 / page_count==4 / warnings==[]）；变异（last_page_count 置 0）20 红→恢复 21 绿 PASS。

---

## B. 记忆模块

### P0

- [x] **B1. 记忆写入不填 tenant_id，fail-closed RLS 下写入被拒/读取恒空** ★ ✅ 2026-11-18
  - 完成证据：`Mem0Manager`/`GraphitiManager` 增加构造参数 `tenant_id`，缺省时经新增 `tenant_context.session_tenant_id()` 从会话 GUC 解析（与 RLS 同源）；`MemoryFact`/`KnowledgeEntity`/`EntityEvent` 三处写入显式填 tenant_id；`MemoryManager` 向两个管理器传播 tenant_id（memory_tasks 的 GUC-only 路径自动覆盖）；memory_tasks 三处写入失败日志 warning→error（数据丢失不得静默）。闸门 `test_memory_rls_integration.py` 7 条（基座见 C1）：红基线 6 failed/1 passed，红因实测为 `new row violates row-level security policy for table "memory_facts"`（非基座问题）→ 修后 7 passed。变异×3 串行：删 add_fact tenant_id → 5 failed；删 Graphiti 两处 tenant_id → 2 failed；add_fact 吞 flush 异常 → 1 failed（日志伪报 fact_added）；恢复后全绿。
  - 证据：`backend/app/memory/mem0_manager.py:171-184` MemoryFact 构造无 tenant_id；`graphiti_manager.py:56-62` register_entity 同；RLS 迁移 e8f9a0b1c2d3:56-59 要求 `tenant_id::text=GUC` 且 FORCE，生产角色 ekb_app 无 BYPASSRLS；失败仅记 warning 静默。对比正确写法：`conversation_repository.py:127-131`。
  - 后果：生产多租户下记忆功能整体失效且无感知。测试用 create_all 不建 RLS（conftest.py:203-204）故全绿。
  - 修复点：add_fact/register_entity 及所有记忆写入路径显式填 tenant_id（从会话/上下文解析）；写入失败不得静默。
  - 验收：带真实 RLS 的集成测试（非 create_all），断言租户 A 写入后租户 B 读不到、租户 A 能读到；先打红。

- [x] **B2. 向量维度错位残余：saas 模式 3072 维 vs 列宽 1024** ★ ✅ 2026-11-18
  - 完成证据：新增 `embedder.memory_embedding_dim()`（saas→OpenAIEmbedder.dim 3072 / saas_dashscope→DASHSCOPE_EMBED_DIM / private_*→TEIEmbedder.dim 1024，与注册表一一对应但不实例化客户端）；`models/memory.py` 列宽改用该函数；新迁移 `e5f6a7b8c9d1` 把存量错宽列重建为当前模式维度（**实测发现 pgvector ivfflat 上限 2000 维**，3072 维列不建 ivfflat，按 user_id 过滤后精确扫描）；`_cosine_similarity` 维度不一致显式 raise ValueError，不再 zip 截断。闸门 `test_memory_vector_dim.py` 13 条：跨 4 模式**子进程真实 PG 建表**读列宽 vs 生产 embedder 注册表实例 dim（两侧不同源）+ 生产迁移 upgrade 真实执行 + JSONB 降级打红；红基线 3 failed（cosine 截断/JSONB 截断/saas 列宽 1024≠3072）→ 修后全绿；`test_pgvector_migration.py` 末条对齐新契约。变异×4 串行：维度函数退回 DASHSCOPE 单配置 → 2 failed；删维度校验 → 2 failed；模型绕过函数 → 1 failed；迁移删 2000 维护栏 → 1 failed（ProgramLimitExceededError）；恢复后全绿。
  - 证据：`backend/app/models/memory.py:62` `Vector(settings.DASHSCOPE_EMBED_DIM)`（默认 1024，config.py:182）；默认 `DEPLOY_MODE="saas"`（config.py:128）→ OpenAIEmbedder dim=3072（embedder.py:48、182-186 注册表）。上次修复（ba9dd63）只对齐 DashScope 路径。
  - 后果：saas 模式 `flush` 报 dimension mismatch 且不在 try 内（mem0_manager.py:186）；查询侧 pgvector 报错降级 JSONB 后 `zip` 截断**静默算错相似度**（mem0_manager.py:38）。
  - 修复点：记忆向量列宽随 DEPLOY_MODE 对应 embedder.dim（迁移或按模式建列）；降级 zip 前断言维度一致，不一致显式报错不得截断。
  - 验收：跨部署模式参数化测试（saas/saas_dashscope/private_*），断言 embedder.dim == 列宽；维度不一致降级路径打红测试。
  - 备注：切换 DEPLOY_MODE 后需重跑迁移 e5f6a7b8c9d1（列宽迁移期固定，迁移 docstring 已注明）；private_finetuned 已注册 TEI embedder（B10，2026-11-19），列宽 1024 与输出维度一致。

### P1

- [x] **B3. 单租户（tenant_id=NULL）异步记忆全丢** ✅ 2026-11-18
  - 完成证据：`memory_tasks._persist_memory_async` 改为一次 `system_scope(reason="memory_tasks.lookup_conversation_owner")` 查询同时取 `(tenant_id, deleted_at)`，三分支：行不存在/已软删除→跳过（竞态守卫不回归）；tenant 非空→`apply_tenant` 严格租户作用域；tenant NULL→写入体包进 `system_scope(reason="memory_tasks.single_tenant_null_tenant")`（fail-closed 下 NULL 行仅在 '*' 可见/可写，审计留痕）。闸门 `test_memory_single_tenant.py` 4 条（C1 基座 + 任务引擎改绑非 superuser 运行角色，否则 superuser 绕过 RLS 掩盖缺陷）：红基线 1 failed/3 passed（红因实测 `resolve_owner_missing`→`skipped_conversation_gone`）→ 修后 4 passed。变异 M-B3（NULL 分支退回无身份）→1 红→恢复绿。
  - 证据：`backend/tasks/memory_tasks.py:57-87` resolve_tenant_of_row 返回 None → fail-closed 查不到会话 → 按「会话已删除」跳过写入；默认开关开启（config.py:846）。
  - 修复点：区分「会话不存在」与「单租户 NULL」两种情形，后者正常写入。
  - 验收：tenant_id=NULL 会话的记忆写入集成测试。

- [x] **B4. checkpoint 每轮被存根覆写，L2 恢复形同虚设** ✅ 2026-11-18
  - 完成证据：`save_session(agent_state: dict | None = None)`——None 时干净跳过 checkpoint 写入（不再抛 AttributeError 被吞）；`chat_service._persist_assistant_result` 同步降级与 `memory_tasks` 均不再传 `{"iteration":0,...}` 存根；Agent Loop 带真实状态的路径照旧写入（正向证人闸门）。加载路径明确降级为「仅进度参考」：prompt 文本改名「历史进度参考：…仅供了解既往进度，不代表可从中断处继续执行」。闸门 `test_checkpoint_clobber.py` 6 条：红基线 3 failed/3 passed（chat 存根覆写 / None 吞崩 / prompt 谎称恢复）→ 修后 6 passed。变异×4 串行：M-B4a chat 恢复存根→1 红 / M-B4b 任务恢复存根默认值→1 红 / M-B4c 去掉 None 守卫→1 红 / M-B4d prompt 恢复旧文案→1 红；恢复后全绿。
  - 证据：`backend/app/services/chat_service.py:760`、`memory_tasks.py:101` 每轮写 `{"iteration":0}`，冲掉 `agents/base.py:335-345` 的真实状态；加载仅渲染提示语（memory_manager.py:115-119），无真状态恢复。
  - 修复点：chat 路径不得覆写 agent checkpoint；恢复路径真正还原状态或明确降级为「仅摘要」并改名去误导。
  - 验收：agent 写入状态 → chat 一轮 → 断言状态未被冲掉。

- [x] **B5. event_log.append 吞错污染共享事务 + seq 竞态丢事件** ✅ 2026-11-18
  - 完成证据：`append` 的 INSERT+flush 包进 `begin_nested()` savepoint——失败只回滚保存点，共享事务不再进入 aborted；`IntegrityError`（seq 唯一约束冲突）有限重试 8 次，每轮重算 MAX，退避 `0.005*(attempt+1)+random.uniform(0,0.02)`（**实测取证**：确定性退避会把败者同步到同一时刻，每轮仅一路前进，8 路并发 5 次预算必耗尽；加抖动后复现脚本 10/10 收敛）；其余异常保持 return 0 不阻塞主流程契约。闸门 `test_event_log_append.py` 2 条（(session_id,seq) 唯一索引从生产迁移 bd2c3d4e5f70 本体同源执行）：红基线 2 failed（`InFailedSQLTransactionError` 毒死共享事务 / 8 路并发丢 3 事件）→ 修后 2 passed，连跑 5 轮稳定。变异×2 串行：M-B5a 去 savepoint→2 红 / M-B5b 重试预算归 1→1 红；恢复后全绿。既有 `test_event_log.py` 等 76 条不回归。
  - 证据：`backend/app/memory/event_log.py:202-210` 失败仅返回 0，无 savepoint，共享 session 事务 abort 后续操作必挂；`:135-139` seq 竞态 IntegrityError 即丢事件，注释称「可重试」但无重试实现。
  - 修复点：append 用嵌套事务（begin_nested）隔离失败；seq 冲突实现有限重试。
  - 验收：append 失败后同 session 后续操作成功的测试；并发 append 不丢事件的测试。

- [x] **B6. agent_checkpoints / agent_event_logs 无 tenant 列无 RLS** ✅ 2026-11-18
  - 完成证据：两表 ORM 补 `tenant_id` 列；新迁移 `7a8b9c0d1e2f`（接 B2 的 e5f6a7b8c9d1）逐表显式 ADD COLUMN + 按 `session_id→conversations.id` 回填存量租户（task:* 无会话归属留 NULL）+ ENABLE/FORCE RLS + fail-closed `tenant_isolation` 策略（显式语句而非 DO 循环，保证静态覆盖闸门 test_rls_role_split 可检出）；`CheckpointManager`/`EventLogManager` 构造参数 tenant_id 优先、缺省从会话 GUC 解析，写入显式填 tenant，upsert 冲突 `COALESCE` 保留既有归属。闸门 `test_agent_state_rls.py` 4 条（C1 基座：跨租户 load_checkpoint/事件流拒绝 + 无 GUC 写入拒绝/不落库，均带本租户正向证人）+ `test_agent_state_migration.py` 1 条（真实 PG 跑迁移 upgrade：uuid 列/回填/策略/FORCE）：红基线随 B1 批次建立，修后 5 passed。变异×2 串行：M-B6a checkpoint 写入不填租户→1 红 / M-B6b 迁移去 FORCE→1 红；恢复后全绿。既有 `test_task_milestones.py` fake DB 补 `scalar()`（GUC 解析新查询）后 65 条不回归。
  - 证据：`backend/app/models/checkpoint.py:29-51`；隔离全靠上游归属校验（chat_service.py:186-188）。
  - 验证方法：跨租户 session_id 直调 load_checkpoint 是否越权。
  - 修复点：补 tenant 列 + RLS（与 B1 同一迁移批次）。

### P2（断链/未接线功能）

- [x] **B7. 混合恢复未接线：event_log 生产零消费** ✅ 2026-11-19（决策：接线，用户已批准）
  - 完成证据：三处接线缺一不可 —— ① `chat_service.stream_chat` 的 engine.answer 迭代包 `event_log_scope(EventLogManager(...))`，trace_node 经 contextvar 拾取真实写节点事件；单租户 NULL 会话由调用方显式传 `null_tenant_reason="chat_service.single_tenant_event_log"` 走审计 '*' 通道（EventLogManager 新增该关键字参数，默认 None 时保持 B6 契约「无 GUC 写入被拒」不回归）；② `save_session` 改走 `save_checkpoint_with_event_log`（快照嵌 `_base_seq`，单租户 NULL 写入包 `system_scope(reason="memory_manager.single_tenant_checkpoint_save")`，多租户 GUC 不受扰）；③ `_load_checkpoint_isolated` 改走 `load_checkpoint_with_event_log`（重放 base_seq 之后增量），构造未传 tenant 时传播请求会话 GUC，NULL 分支显式 `system_scope(reason="memory_manager.single_tenant_checkpoint_load")`。闸门 `test_b7_hybrid_recovery.py` 5 条（C1 基座 + chat 编排 test_chat_disconnect 同款替身，事件/checkpoint 全真实 DB）：红基线 5 failed（事件恒空 ×2 / 快照无 _base_seq / 只读快照不重放 ['d1']≠['d1','d2'] / NULL checkpoint 隐形）→ 修后 5 passed。变异×5 串行：M-B7a chat 去 scope→2 红 / M-B7b 退回裸 save_checkpoint→1 红 / M-B7c 加载不重放→1 红 / M-B7d 单租户事件通道归 None→1 红 / M-B7e NULL 加载去 '*'→1 红；恢复后全绿。既有 B3–B6 闸门（test_agent_state_rls / test_event_log / test_event_log_append / test_memory_single_tenant / test_chat_disconnect / test_checkpoint_clobber）61 条不回归。
  - 证据：`save/load_checkpoint_with_event_log` 仅测试调用；`agent_event_logs` 恒空；消费方 `delivery_chain.py:226` 查空表。
  - 修复点：主链路接线或显式下线该能力（二选一，不留僵尸代码）。
- [x] **B8. build_context 忽略 recent_messages，ctx.short_term 恒空** ✅ 2026-11-19（决策：删死代码，用户已批准）
  - 完成证据：删除全部死 L1 面 —— `build_context` 的 recent_messages 参数、`MemoryContext.short_term` 字段、`to_system_prompt(render_short_term=...)` 参数与「近期对话」渲染块、`SHORT_TERM_WINDOW_SIZE`/`_SHORT_TERM_INJECT_SIZE`/`_SHORT_TERM_MSG_MAX_CHARS` 三常量、to_dict 的 short_term 键、日志 short_term_count；两个调用方（chat_service / agents.base）同步删参；chat 历史注入去掉恒真条件 `if not (memory_ctx and memory_ctx.short_term)`，L1 职责唯一归 chat 热层（Redis 优先、miss 回填 PG、P3-C 摘要压缩链路原样保留）。闸门 `test_b8_dead_short_term.py` 3 条：红基线 1 failed（删除契约）+ 2 绿正向证人（L2/L3/L4 组装渲染不回归 / chat 历史注入含当前消息排除不回归）→ 修后 3 passed。变异 M-B8a 复活 short_term 字段→1 红；恢复绿。既有测试对齐新契约：test_p1_token_optimization 删 6 条死 L1 渲染测试、short_term 优先测试改写为 Redis 热层命中不查 DB（同意图活契约）；test_memory_upgrade 3 条隔离会话测试 mock 改 load_checkpoint_with_event_log；test_memory_sidecar 删死参。受影响 6 套件 104 passed。
  - 证据：`memory_manager.py:205-322`；历史实际靠 Redis 兜底（chat_service.py:1075-1103）；`SHORT_TERM_WINDOW_SIZE:33` 死代码。
  - 修复点：接线或删除死参数，与 Redis 兜底职责划清。
- [x] **B9. 用户纠错/偏好管理无生产入口** ✅ 2026-11-19（决策：补 API + 统一定时，用户已批准）
  - 完成证据：新路由 `app/api/v1/memory.py`（prefix=/memory，已注册）—— PUT /preferences（MemoryManager.set_preference：Mem0 事实 + Graphiti 时间线）、POST /facts/{id}/correct（**路由层属主校验** id+user_id，他人事实 404 不泄露存在性；correct_fact 本体不做属主检查）、DELETE /working-memory（working 类停用，偏好不动）。定时清理 `_cleanup_expired_facts_async` 删除自写按租户迭代 SQL，统一委托 `Mem0Manager(session).cleanup_expired()`（跨租户会话一次扫全表：退场语义 forgotten_reason='expired'+激活值快照与手动清理同源，单租户 NULL 行不再漏清）。`graphiti_manager.record_event` 落库后 best-effort `sync_to_graph`（内部吞错不阻塞，Neo4j 可选后端自此有真实写入方）。闸门 `test_b9_memory_api.py` 6 条（ASGITransport + 真实 app 角色会话租户 A GUC）：红基线 5 failed（三入口 404 / NULL 行漏清 1≠2 / 图同步 0 调用）+ 1 平凡绿（跨用户 404 巧合，由 M-B9a 变异证明判据有效）→ 修后 6 passed。变异×3 串行：M-B9a 删属主条件→1 红 / M-B9b 清理缺 forgotten_reason→1 红 / M-B9c 断图同步→1 红；恢复后全绿。既有面（test_preference_graphiti_sync / test_mem0_conflict_correction / test_memory_rls_integration / test_api_endpoints / test_memory_verdict_persistence / test_analytics / test_knowledge_compounding）115 条不回归。
  - 证据：`correct_fact`/`set_preference`/`clear_working_memory`/`Mem0Manager.cleanup_expired`/`sync_to_graph` 无生产调用方；Graphiti 偏好时间线永不写入。
  - 修复点：补 API 入口或下线；cleanup_expired 与定时任务（scheduled_tasks.py:548-570）二选一统一。
- [x] **B10. private_finetuned 模式未注册 embedder，记忆退化纯关键词** ✅ 2026-11-19（决策：注册 TEI，用户已批准）
  - 完成证据：`_make_tei_embedder` 增加 `@register_embedder("private_finetuned")` —— 该模式的「微调」仅针对 LLM（vLLM 多 LoRA），embedding 与其他私有模式一致走 TEI/BGE-M3；`memory_embedding_dim` 的 TEI 兜底从「暂按」转正式契约（docstring 同步，B2 声明测试改述）。闸门 `test_b10_finetuned_embedder.py` 3 条：红基线 2 failed（ValueError「不支持的 DEPLOY_MODE」）+ 1 绿正向证人（既有四模式注册不破坏）→ 修后 3 passed。变异 M-B10a 摘注册→2 红；恢复绿。B2 闸门 test_memory_vector_dim 13 条不回归。
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

- [x] **C1. RLS 集成测试基座**：现有 conftest create_all 不建 RLS（conftest.py:203-204），B1/B6 的验收依赖一个能真实执行 RLS 策略的测试 fixture。（2026-11-18：`tests/rls_harness.py` 落地 —— create_all 建表 + **加载生产迁移 e8f9a0b1c2d3 的策略 SQL**（非手抄）+ `provision_app_role.provision` 建非 superuser/NOBYPASSRLS 运行角色（ekb_rls_mem_pytest）+ NullPool 应用角色引擎；B1 的 7 条闸门已在其上实测打红→转绿，B6 可直接复用）
- [x] **C2. 真实 PDF 端到端闸门**：即 A17，A1-A3 修完必须联合跑通。（2026-11-18：A1-A9 闸门 23 条 + P2 闸门 39 条全绿；存量 20 样例已接入 `TestA17RealSamples` 并实测通过）
- [ ] **C3. chord 入口真实调用测试**：即 A1 验收，杜绝「函数从未被真实调用」类缺陷再次全绿存活。（2026-11-18：`_parse_and_chunk_async` 已有真实调用集成测试，process_document 任务级入口仍走 mock）

---

## D. 解析策略对齐（2026-11-19，knowhere 对比调研产出）

> 来源：2026-11-19 双代理逐条取证 knowhere 六条设计声明 + 本项目现状核查（knowhere 侧 6 条中仅「资产一等公民」完全属实，详见当日会话记录）。
> 行号以 2026-11-19 HEAD 4547bae 为准，修复前先核对漂移。DoD 与 A/B 组相同：测试通过＋闸门先打红一次＋串行变异自证。
> 决策点默认值清单（一句「按默认值」＝按本清单整体授权，范围仅限 D 组点名文件）：
> - **D3**：Docling 保持主路径，显式传 PdfPipelineOptions（OCR 自动、表格模式可配），不做文本层预检路由（默认）。
> - **D4**：补 xlrd>=2.0.1 直读 .xls（默认）；不走 LibreOffice 转换。
> - **D5**：.doc/.ppt 维持显式拒绝（默认）；LibreOffice 转换轨仅在有真实业务需求时另行立项。
> - **D6**：完整版资产化（2026-11-19 用户确认产品需求：答案需展示图表/图片本身）——图片/表格独立 chunk＋回链，图片上传常开；不做 knowhere 式双向 connect_to 图模型（本项目为 chunk RAG，parent 回链够用）。
> - **D7**：开关下探先做租户级（默认），任务级透传随后。
> - **D9**：PPTX 嵌入媒体仅检测+告警（默认）；提取走 ASR/VLM 为可选升级，另行批准。
> - **D10**：资产描述归属——VLM 描述归图片 chunk 作检索文本，正文留占位引用＋回链（默认，避免双份 embedding 与重复命中）；presigned URL 在下发时解析而非存储时（默认，防缓存答案里 URL 过期）。
> - **D11**：存量回填为显式命令、按需执行（默认），不自动全量重索引。
> 建议顺序：D1→D4（小修、已实锤）→ D2 → D3 → D8 → D9 → D6→D10（资产链路，含迁移）→ D11（回填）→ D7；D5 默认不做。

### P1

- [x] **D1. VLM 失败字符串被当图片描述嵌入正文，污染检索** ✅ 2026-11-19
  - 完成证据：provider 双实现（Anthropic/vLLM）失败返回 None（抽象签名改 `str | None`）；understand_structured 遇 None 产 status=error/low_confidence 空描述字典；PDF/DOCX/PPTX `_vlm_describe` None→跳过+`last_parse_warnings` 留痕（三解析器补 `__init__` 初始化，管线 document_tasks.py:1694 getattr 自动拾取 DOCX/PPTX 警告）；PDF 扫描页 OCR None→空+页码 warning；multimodal_service._safe_understand 失败文案改空串。闸门 `test_d1_vlm_failure_isolation.py` 9 条（真实 provider 代码+LLM/client 抛错替身+真实 fitz 含图 PDF 端到端+正常描述正向证人）：红基线 9 failed → 修后 9 passed。变异×3 串行：M-D1a Anthropic 退回错误字符串→7 红 / M-D1b vLLM 退回→1 红 / M-D1c 禁 PDF None 守卫→2 红；恢复后全绿。既有 test_multimodal_service::test_vlm_failure_graceful_degradation 断言旧污染契约（「失败」in description），按同意图对齐新契约（降级不抛异常+空描述）；受影响 7 套件合跑 362 passed。
  - 证据：`backend/app/vlm/provider.py:135/197` 出错返回字符串「[图像理解失败:…]」；调用方 `pdf_parser.py:499`、`docx_parser.py:698`、`pptx_parser.py:542` 不区分失败，直接拼入正文 HTML → 入 chunk/向量。与 A 组「静默失败」同族。
  - 修复点：provider 失败返回 None（或抛专用异常），解析器跳过描述、保留图片引用并记 parse_warning；失败文案不得进入 content_text/chunk。
  - 验收：打桩 VLM 抛错 → 断言产物不含「图像理解失败」且 warning 存在；正向证人：VLM 正常时描述照常嵌入。变异（失败仍返回字符串）须打红。

- [x] **D2. PDF/DOCX 目录（TOC/书签）真值零利用** ✅ 2026-11-19
  - 证据：全库 grep `get_toc`/outline/bookmark 无命中；legacy PDF 完全不产标题层级（纯文本→分块）；DOCX 仅靠 pStyle HeadingN（`docx_parser.py:344-409`），无样式标题的文档层级全丢。knowhere PDF 轨以 outline 树确定性锚定（其 `skeleton_extractor.py:86-108`），可借鉴。
  - 修复点：PDF 用 fitz `get_toc()` 书签锚定标题层级（按页码/位置注入 h 标签）；DOCX 读 outlineLvl/TOC 域作真值，HeadingN 样式降为补充。无 TOC 时维持现状，不引入 LLM 猜层级。
  - 验收：带书签真实 PDF → 标题层级与书签一致；带 TOC 域但无 Heading 样式的 DOCX → 仍产层级；无 TOC 样例行为不回归（正向证人）。
  - 实现：`pdf_parser.py` get_toc(simple=True) → 页码→[(层级,标题)] 映射，按页注入 <h1>-<h6>（层级钳 1-6、页码钳实际页数）；正文重复标题行去重（`_dedup_toc_lines`，每标题至多剔一次）；标题字符计入扫描判定（长标题章节页不误触发 OCR）。`docx_parser.py` 三级判定：outlineLvl（val 0-8→h1-h6）> HeadingN 样式 > TOC 域条目（TOCN 样式，标题取首个 w:tab 前、剔页码尾巴）精确匹配锚定；TOC listing 自身不产标题。开关 `PDF_TOC_HEADINGS_ENABLED` / `DOCX_OUTLINE_TRUTH_ENABLED` 默认 True，关闭维持现状。全程无 LLM。
  - 证据：闸门 `tests/test_d2_toc_truth.py` 11 条（真实 fitz set_toc PDF 5 条 + 真实 python-docx outlineLvl/TOC 域 DOCX 6 条，含 3 条正向证人）；红基线 8 failed / 3 passed（证人按设计绿）→ 修复后 11 绿；回归 425 passed（document_parser / pdf_pipeline_hardening / chunk_optimization / document_tasks_chunker / parse_router / D1 / D4 / multimodal）。
  - 变异（串行 6 条全打红后还原）：M1 去 PDF 标题注入→3 红；M2 去正文去重→去重判据红（count==2）；M3 PDF 开关恒 True→开关判据红；M4 outlineLvl 真值层失效→2 红；M5 TOC 锚定失效→锚定判据红；M6 TOC 页码尾巴不剔（break→continue）→锚定判据红。终验 425 passed。
  - 校准：heading-only 页闸门初红——原标题 27 字符低于 32 扫描阈值，触发 OCR 属既有阈值语义非 D2 缺陷；标题加长至 34 字符复绿（闸门假设修正，未动产线）。

- [x] **D3. Docling 主路径无参构造，OCR/表格模式不可控** ✅ 2026-11-19
  - 证据：`docling_parser.py:81` `DocumentConverter()` 无参；默认 `DOCLING_ENABLED=True`（`config.py:307`），PDF 无条件跑版面+TableFormer+OCR；legacy 路径反而有文本层预检（`pdf_parser.py:163`，32 字符阈值）。
  - 修复点（按默认值）：显式传 `PdfPipelineOptions`——do_ocr 保持自动（仅扫描页触发）、新增 `DOCLING_TABLE_MODE`（FAST/ACCURATE，默认 FAST）与 `DOCLING_DO_OCR` 开关；不做文本层预检路由（避免 legacy/Docling 双轨质量分叉）。
  - 验收：构造参数断言测试（options 真实传入 converter）；开关关闭时 OCR 管线不实例化。既有 A9 线程闸门不回归。
  - 实现：`_get_converter` 显式构造 `PdfPipelineOptions(do_ocr, do_table_structure=True, table_structure_options=TableStructureOptions(mode))`，经 `format_options={InputFormat.PDF: PdfFormatOption(...)}` 传入 converter；模式非法值回落 FAST；初始化日志带 table_mode/do_ocr。实测库默认 mode=ACCURATE（docling 2.95.0），FAST 为真实降本变更。
  - 证据：闸门 `tests/test_d3_docling_options.py` 6 条；红基线 5 failed / 1 passed（接线证人按设计绿）→ 修复后 6 绿；回归 431 passed（含 A9 线程闸门 2 条）。
  - 变异（串行 4 条全打红后还原）：M1 默认模式翻转→3 红；M2 do_ocr 恒 True→OCR 判据红；M3 表格模式配置失效→ACCURATE 判据红；M4 options 不传 converter（回到无参）→4 红。终验 431 passed。
  - 校准（环境限制，非闸门假设错）：本机 torch 动态库缺失致 `docling.document_converter` 真实导入链断（见范围外勘误 2026-11-19），converter 边界用捕获替身；断言侧用真实 `docling.datamodel` pydantic 模型/枚举（可独立导入，非同源镜像）。「开关关闭时 OCR 管线不实例化」只能断言到 options.do_ocr=False 层，真实管线实例化验证待 torch 环境修复后补。

- [x] **D4. .xls 降级路径必炸：xlrd 缺失＋sniff OLE2 子类型恒误判（本机已实锤）** ✅ 2026-11-19
  - 勘误（立案假设修正）：原证据称「sniff 放行 .xls → 上传能进、解析必败」**不成立**。实测取证：OLE2 目录流名为 UTF-16LE 且目录扇区在文件尾部（5.6KB 样例 Workbook 名在偏移 5248），`_sniff_ole` 却用 ASCII `b"Workbook"` 搜前 1024 字节 → 真实 .xls 恒判 ole2，**上传口即被拒**，到不了解析。双重缺陷同属 .xls 可用路径，一并修复。
  - 完成证据：① sniff `_sniff_ole` 改全字节 UTF-16LE 标记搜索＋Root Entry ±4KB 目录区锚定，WordDocument/PowerPoint Document 优先于 Workbook 判定（防正文含 "Workbook" 字样的 doc/ppt 误判）；② requirements.txt 补 `xlrd>=2.0.1` 并安装入 venv；③ 新增真实 BIFF8 样例 `test_documents/xls/01_员工花名册.xls`（xlwt 临时生成后卸载，不留未声明依赖）。闸门 `test_d4_xls_fallback.py` 8 条（真实样例 sniff 放行/解析非空含两 sheet、doc/ppt 仍拒正向证人、晚目录标记、损坏文件诚实返空、xlsx 主路径不回归、requirements 声明证人）：红基线 4 failed/4 passed → 修后 8 passed。变异×3 串行：M-D4a 嗅探退回 ASCII→2 红 / M-D4b pandas 强制 openpyxl 引擎→1 红 / M-D4c 撤 xlrd 声明→1 红；恢复后全绿。受影响合跑（D4+sniff+document_parser+D1）268 passed。
  - 证据：`factory.py:38` xls→XLSXParser 别名；openpyxl 不支持 .xls → `xlsx_parser.py:159` pandas.read_excel 兜底；pandas 读 .xls 依赖 xlrd，`requirements.txt` 无 xlrd；2026-11-19 实测 `backend/.venv/bin/python -c "import xlrd"` → ModuleNotFoundError。
  - 修复点（按默认值）：requirements.txt 补 `xlrd>=2.0.1`（仅支持 .xls，正合用途）；.xls 解析失败不得静默空（对齐 A2 契约）。
  - 验收：真实 .xls 样例解析出非空表格文本；缺 xlrd 环境打红（import 守卫或 skip 声明）。

### P2

- [ ] **D5. .doc/.ppt 无转换轨（决策点，默认不做）**
  - 证据：`sniff.py:44` ole2 ∈ NON_DOCUMENT_FORMATS → 上传即拒；`documents.py:1522` 存量文档 warning 提示转 OOXML；`factory.py:42-44` 注释显式不注册。行为诚实，非缺陷。
  - 决策：若业务需要，引入 LibreOffice headless 子进程转换（.doc/.ppt/.xls→OOXML，超时+进程隔离+字体依赖，参考 knowhere `office_compat_normalizer.py:48`）；转换有损（嵌入对象/复杂文本框/字体替换），需 warning 留痕。默认维持拒绝，本条挂起待业务输入。

- [x] **D6. 图片/表格资产化：独立 chunk＋回链＋上传常开（2026-11-19 升级为完整版，产品需求＝答案展示图表/图片本身）** ✅ 2026-11-19
  - 证据（三处断点，2026-11-19 实证）：① 图片上传默认关（`config.py:255` PDF_IMAGE_UPLOAD=False，DOCX/PPTX 同构），正文只留「[图片描述:…]」；② `models/chunk.py:27-53` DocumentChunk 无 chunk_type/资产引用/parent 字段（parent_id 仅在内存 dataclass）；③ 图片/表格内联正文 HTML（`base.py:157-165`），无独立检索单元标记。knowhere 资产独立 chunk＋双向边属实，但双向图边服务于其 Agentic 导航检索，本项目 chunk RAG 不搬图模型，只搬「可单独命中＋回链」原则。
  - 修复点：a) `PDF_IMAGE_UPLOAD`/DOCX/PPTX 图片上传默认改 True（存储 key 沿用 A7 命名空间）；b) DocumentChunk 增 `chunk_type`（text/image/table）＋`asset_ref`（图片=minio key，表格=内联 HTML）＋`parent_chunk_id` 回链，新迁移（revision ID 先对全量 versions 查重，吸取 B6 撞号教训）；c) chunker 产出资产 chunk：图片 chunk 文本=VLM 描述（D10 归属规则），表格 chunk 沿用 A6 原子表格＋类型标记；d) 检索侧资产 chunk 正常入向量索引，支持 chunk_type 过滤。
  - 验收：含图/表真实文档端到端 → 资产 chunk 独立存在、类型正确、parent 回链正确；图片描述查询单独命中图片 chunk；正文 chunk 不重复含整表/整段描述（对齐 A13/D10 去重契约）；迁移真实 upgrade 闸门；既有 A6/A13 闸门不回归。
  - 实现：a) PDF/DOCX/PPTX 图片上传默认 True（config 三开关）；b) DocumentChunk 增 chunk_type/asset_ref/parent_chunk_id＋手写迁移 `b7c8d9e0f1a2`（down_revision=a9b8c7d6e5f4，ID 先对全量 versions 查重：e5f6a7b8c9d0/d6e7f8a9b0c1 均已占用被否；IF NOT EXISTS 幂等＋FK 守卫＋存量回填 'text'）；c) chunker 新增 `extract_asset_chunks` **后处理**（chunk() 本体不动，A6 原子表格/确定性 ID 契约不受扰）：表格→独立 table 块（asset_ref=完整 HTML）、minio 图片→独立 image 块（文本=纯 VLM 描述，剥「[图片描述:…]」包装，无描述占位「[图片内容]」），正文块留 [表格]/[图片] 标记；asset_ref 跨父子重复去重；仅发生抽取时重排确定性 ID（纯文本输入原样返回，幂等）；d) init_chunks/_chunk_document 接入抽取＋parent_chunk_id 回链落库；三向量库（OS/Milvus/Qdrant）upsert/检索/fetch 全链携带 chunk_type/asset_ref，filter_builder 支持 chunk_type 单值/多值过滤。
  - 证据：闸门 `tests/test_d6_asset_chunks.py` 19 条（真实 fitz 图＋表 PDF 走真实 PDFParser，MinIO/VLM 边界替身；迁移走真实 alembic upgrade/downgrade）；红基线 17 failed＋2 errors（迁移 fixture 未建）→ 修复后 19 绿；21 文件全量回归 **698 passed / 0 failed**（741.71s；上轮 696 passed＋2 条旧契约失败，对齐后转绿）。
  - 变异（串行 7 条全打红自身判据后还原）：M1 上传默认回 False→上传闸门红（assert False is True）/ M2 删 seen 去重→父子去重闸门红 / M3 `if assets or changed` 退回 `if assets`→父块残留整表判据红 / M4 删描述包装剥离→「图片块文本＝纯 VLM 描述」红 / M5 删回链落库→parent_chunk_id 判据红 / M6 键集合删 chunk_type→validate_filters＋常量闸门 2 红 / M7 OS upsert 体删资产字段→KeyError 'chunk_type' 红。终验 698 passed。
  - 校准：① 管线闸门初版假设错——`_parse_and_chunk_async` 返回 `{chunk_objects, graph_enabled, parse_warnings}`，embedding/索引在外层调用方而非函数内；闸门改断言 chunk_objects（假设修正，未动产线）；② 闸门抓出真缺陷两处并在 D6 范围内修复：VLM 描述带「[图片描述:…]」包装须剥离；父块资产全被去重时替换后正文被丢弃致整表泄漏（changed 标志修复）；③ 2 条旧契约测试对齐（B8/B9 先例）：test_chunks 的 SemanticChunker mock 补 extract_asset_chunks 透传、test_filter_builder 键集合常量补 chunk_type，断言意图不变。
  - 部署注意：迁移待 `alembic upgrade head`；存量文档资产 chunk 需 D11 显式回填（hash 早退不自然触发）；Milvus 动态 schema 新字段随 upsert 自动带入（与层级字段同模式），OS 旧索引走动态映射（chunk_type 单词小写值 term 过滤两态皆可用），Qdrant payload 无 schema。

- [x] **D7. 解析成本开关无租户/任务粒度** ✅ 2026-11-19
  - 证据：`config.py:251-323` 全部全局 env；租户级仅模块开关（`app/core/modules.py`）；任务级 content_type 参数存在但调用方恒传 auto（`document_tasks.py:781/2219`）。knowhere 为任务级请求参数（其 `job.py:56-59`），无租户级。
  - 修复点（按默认值）：先做租户级解析配置覆盖（VLM 描述/OCR/MinerU/表格模式），复用 modules 的租户配置模式；任务级透传随后。全局 env 降为缺省值。
  - 验收：租户 A 关 VLM 描述、租户 B 开 → 同文档解析行为分叉的集成测试；未配置租户回落全局默认（正向证人）。
  - 实现：a) 新增 `app/core/parse_config.py` — 白名单 8 键（7 布尔：PDF/DOCX/PPTX_IMAGE_EXTRACTION_ENABLED、DOCLING_VLM_IMAGE_ENHANCE、PDF_SCAN_OCR_ENABLED、DOCLING_DO_OCR、MINERU_ENABLED ＋ DOCLING_TABLE_MODE 仅 FAST/ACCURATE 大小写规范化）；`validate_parse_config` 管理端写入边界；contextvar `parse_overrides()` 上下文管理器（token reset 退出必还原）；`get_parse_settings()` 无覆盖返回全局 Settings 本体、有覆盖返回覆盖优先代理。DOCLING_ENABLED 路由开关与 MinIO 上传开关刻意不入白名单。b) `TenantService.get_parse_config/update_parse_config` — 复用 modules 的 Tenant.settings JSONB 模式（`parse_config` 键），函数式构造新 dict 防裸 JSONB 变更检测丢失（同 update_enabled_modules 注释教训）。c) `GET/PUT /api/v1/tenants/parse-config` — PUT 仅 admin（403）、按 user.tenant_id 防跨租户、非法键/值 400，镜像 modules 端点安全模式。d) 管线两入口（_parse_and_chunk_async / _process_document_async）按 doc.tenant_id 加载配置后以 parse_overrides 包裹 _parse_document。e) 解析器 6 处消费点改读 get_parse_settings()：pdf_parser:103、docx_parser:80、pptx_parser:96、docling_parser:89（表格模式/do_ocr）/:162（VLM）/:320（MinerU 开关）；docling:307/:388（MINERU_PYTHON/BACKEND 非白名单）、xlsx（XLSX_* 键）、image_storage、factory 保持全局。factory 每次返回新解析器实例 → DoclingParser converter 缓存按实例隔离，无跨租户缓存污染。
  - 证据：闸门 `tests/test_d7_tenant_parse_config.py` 15 条（校验 3/服务落库 2 含 raw SQL 读回＋custom_key 保留/覆盖代理 2/解析分叉 4 含 VLM A关B开同文档、OCR 分叉、未配置回落证人、A→C 跨租户泄漏/docling+mineru 3/API 1 复合）；红基线 14 failed / 1 passed（回落证人按设计绿；红因 ModuleNotFoundError parse_config、AttributeError update_parse_config、端点 404，均为缺契约红）→ 实现后 15 绿；28 文件全量回归 **795 passed / 0 failed**（54.64s；780 基线＋D7 15；首轮 13 红为闸门假设错位，对齐后复绿，见校准④）。
  - 变异（串行 9 条全打红自身判据后还原）：M1 覆盖代理恒回落全局→6 红（覆盖优先＋五分叉闸）/ M2 上下文退出不 reset→reset 判据红（泄漏闸仍绿：管线每调用自设上下文，该闸靶是管线加载而非 reset——M2 命中自身靶 reset 断言）/ M3 白名单放行未知键→2 红（校验＋API 400）/ M4 update 覆写整个 settings→custom_key 保留判据红（raw SQL 读回）/ M5 管线跳过租户配置加载→VLM/OCR 分叉 2 红（验收判据）/ M6 pdf_parser 退回 get_settings→VLM/OCR 分叉 2 红 / M7 API 放行非 admin→editor 403 判据红 / M8 表格模式不规范化→规范化判据红 / M9 统一入口无视 contextvar→6 红。终验 15 passed。
  - 校准：① 本机 torch 动态库缺失（既有勘误桶）docling 真实导入链断——converter 边界沿用 D3 捕获替身模式，断言侧用真实 docling.datamodel pydantic 模型/枚举（非同源镜像）；② NO_PROXY 含 ::1 炸 httpx（既有环境勘误）——闸门 autouse delenv＋API 闸门 ASGITransport trust_env=False；③ MinerU 分叉闸门 MINERU_PYTHON 指假路径＋真实扫描 PDF，_should_use_mineru 三态（租户开/租户关/全局关证人）不依赖真实 MinerU 安装；④ 首轮 28 文件回归 13 failed / 782 passed——逐条取证全属闸门假设错位（非产品缺陷，B8/B9 先例对齐）：a) 4 条解析器闸门 patch 旧符号 `pdf_parser/docx_parser.get_settings`，D7 消费点改读 get_parse_settings 后 patch 失效→改 patch 新入口（断言意图不变）；另 2 条扫描 OCR 闸门 patch 靶同样失效（disabled 闸此前靠 OCR 边界静默失败巧合绿，全局默认 SCAN_OCR=True 取证见 config.py:261）一并对齐；b) 9 条管线闸门（upload_summary 7＋chunker 2）mock doc 无真实 tenant_id（MagicMock），D7 新增租户配置查询 asyncpg UUID 编码炸（task_db_session 走真实测试库）→补 `mock_doc.tenant_id=None`（私有部署语义→回落全局，零行为变化）。对齐后受影响 4 文件 354 passed。
  - 部署注意：无新迁移（复用 Tenant.settings JSONB）；未配置租户行为与全局 env 完全一致（零行为变化上线）；任务级透传按默认决策挂起随后；管理端经 PUT /api/v1/tenants/parse-config 配置。

- [x] **D8. 解析路径/质量元数据不落库，降级无留痕** ✅ 2026-11-19
  - 证据：parser_type（docling/legacy）仅在日志；无 extraction_status 类字段；A2/A8 的 warnings 已落 doc.parse_warnings，但「最终走了哪条引擎、是否降级」不可回溯。
  - 修复点：Document 增 parse_meta（引擎、降级链、耗时、OCR 页数），解析完成时写入；重索引（A14）时同步刷新。
  - 验收：Docling 失败降级 legacy 的样例 → parse_meta 记录两级链路；正常路径记录单引擎（正向证人）。
  - 实现：`Document.parse_meta` JSONB 列 + 手写迁移 `a9b8c7d6e5f4`（down_revision=7a8b9c0d1e2f，ID 先查重：b4c5d6e7f8a9/f1a2b3c4d5e6 均已占用被否）；`_parse_document` 计时并组装 chain/engine/duration_ms，异常路径也留痕（failed=True）；`_rec_meta` 在 dispatch/_try_docling_parse/_parse_pdf 逐级记录（docling ok/empty/error/unavailable → legacy:pdf_enhanced → legacy:pdf_plaintext，video/audio/passthrough 各留痕）；PDFParser 暴露 `last_scan_ocr_count` → meta.ocr_pages。哈希未变早退保留旧 meta，重解析刷新（A14 语义）。
  - 证据：闸门 `tests/test_d8_parse_meta.py` 10 条（真实 fitz PDF 走真实 legacy PDFParser，Docling 边界替身）；红基线 9 failed / 1 passed（早退证人按设计绿）→ 修复后 10 绿；回归 533 passed（含 A14 重索引闸门、upload_summary、pdf_p2_hardening）。
  - 变异（串行 5 条全打红后还原）：M1 成功路径 meta 不落 doc→7 红；M2 docling empty 不留痕→链路判据红；M3 error 不留痕→error 判据红；M4 OCR 页数恒 0→ocr 判据红；M5 早退误清 meta→早退判据红。终验 533 passed。
  - 校准：① 闸门边界初版未关 factory._is_docling_enabled，legacy 层 get_parser 仍路由到 DoclingParser（包可导入但 converter 断）致三级链——闸门补关判定后 legacy 走真实 PDFParser（边界设置修正，未动产线）；② 既有 A14 spy_dispatch 固定签名不收 meta  kwarg 报 TypeError——按 B8/B9 先例对齐 spy 签名（透传 meta），断言意图不变。
  - 部署注意：迁移待 `alembic upgrade head` 应用；历史文档 parse_meta=NULL，下次重解析回填。

- [x] **D9. PPTX 嵌入音视频被静默忽略，无 warning（决策点，默认仅告警）** ✅ 2026-11-19
  - 证据：`pptx_parser.py` 全文无 media/audio/video 处理（2026-11-19 grep 零命中）；嵌入媒体内容直接丢失且无感知。对比 knowhere：转换前剥离嵌入媒体并记录为独立资产（其 `formats/pptx/embedded_media.py:100` separate_embedded_media，海报保留、媒体入 `embedded_media/` 可检索、时长准入策略）——其瘦身动机是 PPTX→PDF 转换专用，本项目原生解析不适用，但「媒体不得静默丢」的原则适用。
  - 修复点（按默认值）：检测媒体形状（video/audio 关系类型）→ 记 parse_warning「含 N 个嵌入音视频，未解析」，不做提取。可选升级：提取媒体字节走既有 ASR/VLM 管线（复用独立媒体文件链路），另行批准后实施。
  - 验收：含嵌入视频的真实 PPTX → warning 计数正确；纯图文 PPTX 无 warning（正向证人）；不引入转换依赖。
  - 实现：`_count_embedded_media` 遍历 slide.part.rels，reltype 以 /video、/audio、/media 结尾计入，同目标 part 去重（video+media 双关系不重计）；parse 开头重置 last_parse_warnings（复用不残留），计数>0 记 warning + 日志。warning 经既有 getattr 链路汇入 doc.parse_warnings。零转换依赖、零提取。
  - 证据：闸门 `tests/test_d9_pptx_media_warning.py` 6 条（python-pptx add_movie 真实嵌入媒体 part；audio 经 OOXML reltype/videoFile 改写构造）；红基线 4 failed / 2 passed（证人按设计绿）→ 修复后 6 绿；回归 539 passed。
  - 变异（串行 4 条全打红后还原）：M1 检测恒 0→4 红；M2 reltype 过滤失效（全关系计入）→5 红含正向证人；M3 目标去重失效→计数 3 红；M4 parse 不重置 warning→复用残留判据红。终验 539 passed。
  - 校准：双视频闸门初红——相同字节被 python-pptx 合并为同一媒体 part（实测 rels 均指 media1.mp4），文件实含 1 个嵌入媒体，产线去重计数正确；闸门改为每视频互异字节后复绿（样本构造修正，未动产线）。

- [x] **D10. 聊天答案出口无资产通道，图到不了前端（D6 的出口配套）** ✅ 2026-11-19
  - 证据（2026-11-19 实证）：sources 事件仅 `id/title/content[:200]/score`（`app/rag/engine.py:971-983`），无资产类型/URL 字段；`resolve_minio_image_urls` 全库仅文档详情端点调用（`api/v1/documents.py:896-901`，A11 修复范围），聊天/引用卡片路径未接入；content 截断 200 字符会切碎表格 HTML。
  - 修复点（按默认值）：a) sources/引用卡片契约扩展 `chunk_type`＋`asset_url`（图片=presigned URL，表格=完整 HTML 不截断）；b) presigned 在**下发时**解析（复用 A11 的 resolve_minio_image_urls/签名失败保留原址契约），存储层与向量库不存签名 URL（防缓存答案过期）；c) 答案正文引用图片的展示契约与前端对齐（前端渲染改动若不在本仓库，输出接口契约文档）。
  - 验收：检索命中图片 chunk 的问答 → sources 事件含可访问 presigned URL（真实 MinIO 签名）；命中表格 chunk → HTML 完整下发；签名失败降级不炸流（正向证人）；text chunk 行为不回归。
  - 实现：新增 `app/rag/asset_sources.py`（enrich_asset_docs 浅拷贝＋图片 presigned 下发时解析＋表格 content 取 asset_ref 完整 HTML；build_sources_payload 表格不截断/text 保持 [:200]）；engine sources/citations 段抽为 `_emit_sources_events`（run() 原位接线，行为不变）；引用卡片＋chunk_type/asset_url、表格 snippet 不截断；**检索侧两处必要接线**：父子回溯对 image/table 豁免（资产块本身即交付物，否则被父块正文替换＋去重吞并）、BM25 路 _source/解析携带 chunk_type/asset_ref；接口契约 `docs/d10-asset-sources-contract.md`（前端渲染改动不在 D 组范围，按修复点 c 输出契约文档）。
  - 证据：闸门 `tests/test_d10_asset_sources.py` 22 条（含 1 条真实 MinIO 上传→presign→匿名 GET 200 字节一致；签名失败保留原址/不炸流、text 不截断回归、普通子块回溯等 4 条正向证人）；红基线 21 failed / 1 passed（回溯证人按设计绿）→ 修后 22 绿；26 文件全量回归 **768 passed / 0 failed**（计数分解 698＋D10 22＋引用/引擎/p0 48 核实；向量套件因网络 fail-fast 由 741s 降至 37.61s，环境变化非跳测）。
  - 变异（串行 8 条全打红自身判据后还原）：M1 图片不 presign→asset_url 判据红 / M2 表格不取 asset_ref→完整 HTML 判据红 / M3 payload 表格也截断→不截断判据红 / M4 卡片 snippet 截断表格→卡片判据红 / M5 卡片丢资产字段→KeyError 红 / M6 删回溯豁免→资产内容被父块替换红 / M7 BM25 _source 丢字段→请求判据红 / M8 enrich 污染原 docs→「存储层不存签名 URL」不变量红。终验 768 passed。
  - 校准：① 闸门初设回溯后 chunk_id 换父 id——产线保留子块 id（既有契约），闸门对齐；② 表格夹具 content 与 asset_ref 初版同源恒真——content 改摘要文本、asset_ref 为唯一权威源（断言两侧不许同源）；③ 真实签名闸门初只认 SigV4——boto3 s3 默认 SigV2（Signature=/Expires=），MinIO 接受且匿名 GET 200，判据改「真实签名参数存在（V2/V4）＋GET 200」；④ 本机 NO_PROXY 含 ::1 炸 httpx 环境代理解析——闸门 trust_env=False（既有环境勘误桶，非产品缺陷）。
  - 备注（后续项，未混做）：shortcut 通道 sources 形状不同（无 content）尚未携带资产字段；minio_client 默认 SigV2 在 MinIO 可用、AWS 新区域需 V4——均记后续，不顺手修。
  - 部署注意：真实签名闸门依赖本机 MinIO（docker compose minio 服务，凭据同 backend/.env）；Docker Hub minio/minio 已不公开（2025 起），本机用 pgsty/minio 社区镜像打标签 minio/minio:latest 供 compose 使用——生产部署需可达镜像源；26 文件回归基线自此要求 MinIO 在跑。

- [x] **D11. 存量文档资产回填（D6/D10 上线后一次性动作）** ✅ 2026-11-19
  - 证据：A14 按 file_hash 判定重解析——文件未变即早退（`document_tasks.py` _parse_document），存量文档不会自然获得资产 chunk；chunk.py 模块注释明示「存量惰性初始化，不迁移存量向量数据」。
  - 修复点（按默认值）：显式回填命令/管理端点（强制重解析＋重建 chunk，绕过 hash 早退但保留 A14 语义供日常重索引用），按知识库/租户粒度可选执行；不做自动全量重索引（embedding 成本）。
  - 验收：对含图存量文档执行回填 → 资产 chunk 生成、旧 text chunk 不重复残留；未执行回填的文档行为不变（正向证人）；回填幂等（重复执行不产生重复 chunk）。
  - 实现：① `_parse_document` / `_parse_and_chunk_async` 增 keyword-only `force_reparse=False` — 仅跳过「哈希一致」早退，无源文件/文件丢失两处早退不受影响，默认路径 A14 语义不变；② `scripts/backfill_asset_chunks.py`（沿用 backfill_index_classification 模式）：`load_targets` 以 PG 为权威源（排除软删除/无源文件/视频音频/.doc/.ppt，doc_type NULL 视为可回填），`--kb-id/--tenant-id/--doc-id/--dry-run` 粒度可选；`backfill_one` = 强制重解析分块 → 按 doc_id 删三索引旧条目（全文/k-NN 复用生产 `_delete_document_from_indexes`，跨模态单独 delete_by_query — 其 chunk 为随机 UUID ID，不先删会累积重复）→ `_build_index_async` 重建；串行执行＋单文档失败隔离；不走 `_finalize`（不触发审核/发布/智能处理）；批次有失败 exit 1。
  - 证据：闸门 `tests/test_d11_asset_backfill.py` 12 条（force 接线 4 条含 3 正向证人：A14 默认早退/编辑型无文件/源文件丢失；回填命令 3 条：资产生成＋三索引先删后建、幂等确定性 ID、dry-run 零写入；目标选取 3 条：kb/租户粒度＋排除项；隔离 2 条：失败不中断批次、未回填文档内容/哈希/状态/索引事件全不变）；红基线 11 failed / 1 passed（A14 证人按设计绿）→ 修后 12 绿；27 文件全量回归 **780 passed / 0 failed**（46.50s，768＋12）；CLI 实测：ekb_test 上 `--dry-run` 正确列出目标 exit 0，非法 UUID exit 1。
  - 变异（串行 9 条全打红自身判据后还原）：M1 去掉 force 早退绕过→force 判据红（3 红）/ M2 force 默认翻 True→A14 证人红 / M3 跨模态清理指向错误索引→三索引清理判据红 / M4 先重建后删除→顺序判据红 / M5 dry-run 也写入→dry-run 判据红 / M6 丢 kb 过滤→kb 粒度判据红 / M7 丢排除项→排除判据红 / M8 失败中断整批→隔离判据红（首版变异未达靶——解析失败走 status 分支而非异常分支，属等价变异，改异 status 分支后打红）/ M9 force 不透传→force 判据红（3 红）。终验 12 绿＋780 passed。
  - 校准：① 本机 NO_PROXY 含 ::1 使生产 helper 的 httpx client（trust_env 默认开）构造即 InvalidURL——闸门边界内 delenv NO_PROXY（既有环境勘误桶，非 D11 缺陷；脚本自身跨模态 client 用 trust_env=False）；② 本机 dev 库 ekb 不存在（PG 容器只有 ekb_test）——CLI 冒烟经进程内 env 覆写指向 ekb_test，凭据不落盘不回显。
  - 备注（后续项，未混做）：生产 `_delete_document_from_indexes` 只清全文/k-NN 两索引，归档/下架路径同样不清跨模态索引（随机 UUID 条目残留）——2026-11-19 发现，记后续，不顺手修。
  - 部署注意：D6 迁移＋D10 上线后显式执行（建议先 `--dry-run` 核对目标）：`python -m scripts.backfill_asset_chunks --kb-id <UUID>`；每文档走完整重解析＋embedding，串行有成本，按库分批；VECTOR_STORE=milvus 部署索引清理仅覆盖 OpenSearch（脚本输出警告，Milvus 需重新触发向量化任务）。

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

共性根因已实证（2026-11-18，B2 闸门取证时验证，仍未修复、维持范围外）：本机环境变量 `NO_PROXY` 含**裸 `::1`**（`localhost,.localhost,127.0.0.1,127.0.0.0/8,[::1],::1`），httpx 解析 no_proxy 列表时把 `::1` 当 host:port 拆，端口 `':1]'` 抛 `ValueError` → 构造 AsyncOpenAI/httpx 客户端即 `InvalidURL(':1]')`，与 provider/endpoint 配置无关。修复方向二选一：清理本机 NO_PROXY 环境（运维动作），或产品侧客户端构造容错（需单独立项）。测试侧仅在 B2/pgvector 闸门内用 monkeypatch 清理测试进程环境变量（`localhost,127.0.0.1`），不改产品代码。

环境勘误（2026-11-19，D3 取证时发现，未修复、归环境桶）：backend/.venv 的 torch 动态库缺失——`torch/_C.cpython-312-darwin.so` 依赖的 `libtorch_cpu.dylib` 不存在（`torch/lib/` 下被删），`import docling.document_converter` 即 ImportError。症状与 maclean 重复文件去重永久删 venv .so/.dylib 的已知事故吻合（见用户记忆 reference-maclean-cleaner-app）。影响：本机 Docling 主路径实际不可用，`_get_converter` 捕获异常降级 legacy（产品优雅降级按设计生效，非产品缺陷）；D3 闸门只能在 options 层断言。修复方向：重装 torch（运维动作，需授权；本机禁止长占，下载量约百 MB 级）；修复后补「DOCLING_DO_OCR=False 时 OCR 模型不实例化」的真实管线闸门。

本次引入又修复的 4 条回归：
- test_task_lock.py::TestProcessDocumentLockCoverage ×2 —— 测试 fake `_fake_parse(doc_id)` 未接受 A1 新增 tenant_id 关键字，更新 fake 签名（锁行为本身未破坏）。
- test_upload_summary.py::TestDocumentTaskWarnings ×2 —— A2 失败早退返回值丢了 warnings/parse_status/char_count 字段（下游 eval_resume_seed.py:175 与摘要响应依赖），已在两处早退 dict 补齐字段。

B1/B2 轮全量回归（2026-11-18）：55 failed / 5394 passed。其中 52 条与上表既有失败**逐文件条数完全一致**（13+13+8+6+5+3+3+1）；新增 3 条为本次引入回归，已修复：
- test_preference_graphiti_sync.py::TestRecordPreferenceChange ×3 —— mock db 按精确 execute 次数排 side_effect，B1 新增的 GUC 兜底解析多发一次查询耗尽序列（StopAsyncIteration）。取证确认产品行为正确（真实 RLS 闸门已覆盖 GUC 路径），按生产 chat_service 路径给测试显式传 tenant_id 消除兜底查询；复跑 6 passed。

P1 轮（B3–B6）范围外发现（2026-11-18，未修复，归测试体系/迁移桶）：
- `agent_event_logs` 的 `(session_id, seq)` 唯一索引**只存在于迁移 bd2c3d4e5f70**，ORM `models/event_log.py` 无 `__table_args__` 声明 → 一切 `create_all` 基座（conftest/rls_harness）都没有该约束，seq 竞态在这类环境无兜底。生产库经迁移已有索引，B5 重试闸门从迁移本体同源建索引验证；ORM 补声明需单独立项（涉及与迁移索引名对齐）。
- B5 取证副产物（已在 B5 范围内修复，留档备查）：确定性退避把并发败者同步到同一重试时刻，每轮仅一路前进，N 路并发必然耗尽重试预算——退避必须带随机抖动（复现脚本 10/10 收敛验证）。

P1 轮引入又修复的回归：
- test_memory_upgrade.py::TestP0ConversationDeletedGuard::test_writes_when_conversation_alive ×1 —— B3 把会话查询列从 `(id, deleted_at)` 改为 `(tenant_id, deleted_at)`，fake 行（SimpleNamespace）缺 tenant_id 属性。产品行为正确（B3 真实 RLS 闸门覆盖），给两条 fake 行补 tenant_id=None 对齐新契约；复跑 27 passed。
- test_task_milestones.py::TestCheckpointManagerMilestones ×5 —— B6 后 save_checkpoint 发 `SELECT current_setting` 解析租户 GUC，fake `_FakeResult` 缺 `scalar()`。补方法后 65 条全绿（详见 B6 完成证据）。
- 迁移 revision ID 碰撞 ×3（test_external_credentials_migration / test_model_fields_p0p2 / test_upload_summary 的链断言）—— B6 新迁移初版误用 `f6a7b8c9d0e1`，与存量 `2026_07_21_1400-f6a7b8c9d0e1_add_knowledge_compounding_layer.py` 撞 ID，alembic 图报 Cycle。改唯一 ID `7a8b9c0d1e2f` 并重命名文件后 127 条相关测试全绿。教训：新迁移 ID 必须先对全量 versions 查重（闸门只直跑 upgrade 不建图，查不出碰撞）。
- test_tenant_isolation_p4.py::TestRLSMigration ×3 —— fixture `glob("*rls*.py")[0]` 无序选取，B6 新迁移文件名含 `rls` 字样抢占 [0]，断言对象漂移到新文件（revision 链/29 表清单/NO FORCE 断言全红）。fixture 改按 revision ID `*b8c9d0e1f2a3*` 精确选取后该文件 24 passed。取证：全量回归 55 failed 中此 3 条之外恰为 52 条基线（逐文件分布一致）。教训：按模糊 glob 选迁移文件的存量测试，新增同类文件即漂移，选取须锚定 revision ID。

P1 轮范围外发现（2026-11-18，未修复，归 PDF 测试桶）：
- `test_pdf_pipeline_hardening.py` / `test_pdf_p2_hardening.py` 共 7 条（A3×2 / A1A2×2 / A10×2 / A14×1）**未打桩 VLM 边界**：中文 fitz 生成页走扫描 OCR 分支时真实调用 DashScope `qwen-vl-max`。常态环境下 NO_PROXY 裸 `::1` 使 httpx 客户端构造即失败（见上方共性根因），请求发不出去反而恒绿；一旦 NO_PROXY 被清理（如测试 shim `localhost,127.0.0.1`），真实请求发出 → 404 Model not exist → 断言失败。取证：同批测试无 shim 13 passed / 有 shim 7 failed，与本批记忆模块改动无关。修复方向：VLM 边界打桩或显式声明外部依赖（需单独立项）。
- 计量口径备注：既有 52 条基线在**无 shim** 条件下测得；加 NO_PROXY shim 后 52 条 httpx 失败转绿、同时暴露上述 7 条 VLM 盲区。P1 收尾全量回归按无 shim 基线口径对比。

P2 轮闸门校准留痕（2026-11-19，红基线阶段取证定性为闸门自身错误，非产品缺陷，均已修复）：
- B10 正向证人初跑红：TEIEmbedder 构造经 httpx trust_env 解析本机 NO_PROXY 裸 `[::1]` 抛 ValueError（':1]'，与 52 条基线同根因）。闸门加 autouse fixture 剥离 NO_PROXY/no_proxy，与测量环境解耦。
- B8 正向证人初跑红 ×2 因：① `_load_checkpoint_isolated` 走生产全局 `async_session_factory`（指向 dev 库），闸门未改绑 RLS 测试库 → 恒 None；② 种子未 commit 即调 build_context，独立连接读不到未提交行。补工厂绑定 fixture + 先提交后组装。
- B7 重放闸初跑红在种子阶段：事件表空首条 append 必得 seq 1，闸门期望 seq 2 缺一条锚点种子事件；同犯未提交即独立连接读取。补 seq 1 种子 + 先提交。
- B9 API 闸初跑 ERROR ×4：users.tenant_id 外键要求 tenants 行先存在，闸门缺租户种子；补裸 SQL 种子时又踩 ORM 默认值不落 server_default（plan NOT NULL），显式给 plan/max_users/max_storage。教训：裸 SQL 种子不能依赖 ORM 级 default。
- B9 跨用户闸修复前平凡绿（路由不存在时 404 恰合断言）：由 M-B9a 变异（删属主条件→红）证明其修复后真实守护属主判据。

---

## 已完成 / 明确不做

- A1–A9（2026-11-18）：PDF 解析链路 9 条修复完成。闸门 `backend/tests/test_pdf_pipeline_hardening.py` 23 条（红基线 21 failed/2 passed → 修后 23 passed）；12 次串行变异自证全部「打红→恢复→绿」；受影响套件合跑全绿；全量回归仅剩 52 条 HEAD 既有失败（见上方勘误）。
- A10–A17（2026-11-18，P2）：PDF 链路 8 条修复/验收完成。闸门 `backend/tests/test_pdf_p2_hardening.py` 39 条全绿（A15 闸门在 test_sniff.py）；10 次串行变异自证全部「打红→恢复→绿」；既有受影响 7 套件合跑 441 passed；20 个存量真实样例全部解析成功（4 页、886~1217 字、零告警）。
- B1/B2/C1（2026-11-18，记忆模块 P0 + RLS 基座）：闸门 `test_memory_rls_integration.py` 7 条 + `test_memory_vector_dim.py` 13 条 + `test_pgvector_migration.py` 8 条（末条对齐新契约），红基线 9 failed/6 passed → 修后 28 passed；7 次串行变异自证全部「打红→恢复→绿」（M1 删 add_fact tenant→5 红 / M2 删 Graphiti tenant→2 红 / M3 维度函数退单配置→2 红 / M4 删维度校验→2 红 / M5 模型绕过函数→1 红 / M6 删 ivfflat 维护栏→1 红 / M7 吞 flush→1 红）；受影响 24 套件合跑 505 passed/1 既有失败；全量回归 52 failed/5397 passed，失败逐文件分布与既有 52 条基线完全一致（中途 3 条 mock 计数回归已修复，见勘误）。
- B3–B6（2026-11-18，记忆模块 P1）：4 条修复完成，新增闸门 17 条全绿 —— B3 `test_memory_single_tenant.py` 4 条（红基线 1 failed/3 passed，任务引擎须绑非超管 app 角色否则 RLS 被绕过）、B4 `test_checkpoint_clobber.py` 6 条（红基线 3 failed/3 passed）、B5 `test_event_log_append.py` 2 条（红基线 2 failed，修后连跑 ×5 稳定）、B6 `test_agent_state_rls.py` 4 条 + `test_agent_state_migration.py` 1 条（真实 PG 跑生产迁移 upgrade：补列/回填/FORCE RLS）。9 次串行变异自证全部「打红→恢复→绿」（M-B3 NULL 分支退 nullcontext→1 红 / M-B4a chat 恢复存根→1 红 / M-B4b task 恢复存根→1 红 / M-B4c 删 None 守卫→1 红 / M-B4d 恢复旧误导文案→1 红 / M-B5a 删 savepoint→2 红 / M-B5b attempts=1→1 红 / M-B6a checkpoint tenant 恒 None→1 红 / M-B6b 删 FORCE→1 红）。B5 竞态修复含取证：确定性退避会同步化并发 loser（8 worker 实测 seq [0,0,5,0,4,2,1,3]），改 8 次上限+抖动退避后最小复现 10/10 收敛。受影响 27 套件合跑 387 passed/1 xfailed；最终全量回归（无 shim 基线口径）52 failed/5414 passed，失败逐文件分布与既有 52 条基线完全一致，passed 恰为基线 5397+新增闸门 17。本批引入又修复的回归 4 起（fake 行缺 tenant_id ×1 / fake result 缺 scalar ×5 / 迁移 ID 撞号 ×3 / P4 fixture glob 漂移 ×3，见勘误）；范围外新发现 PDF 测试 VLM 边界盲区 ×7（见勘误）。
- B7–B10（2026-11-19，记忆模块 P2 断链/未接线）：4 条修复完成（决策均经用户批准：B7 接线 / B8 删死代码 / B9 补 API+统一定时 / B10 注册 TEI），新增闸门 17 条全绿 —— B7 `test_b7_hybrid_recovery.py` 5 条（红基线 5 failed）、B8 `test_b8_dead_short_term.py` 3 条（红基线 1 failed + 2 绿正向证人）、B9 `test_b9_memory_api.py` 6 条（红基线 5 failed + 1 平凡绿由变异证真）、B10 `test_b10_finetuned_embedder.py` 3 条（红基线 2 failed + 1 绿正向证人）。10 次串行变异自证全部「打红→恢复→绿」（M-B7a 去 chat scope→2 红 / M-B7b 裸 save→1 红 / M-B7c 不重放→1 红 / M-B7d 单租户事件通道归 None→1 红 / M-B7e NULL 加载去 '*'→1 红 / M-B8a 复活僵尸字段→1 红 / M-B9a 删属主条件→1 红 / M-B9b 清理缺退场语义→1 红 / M-B9c 断图同步→1 红 / M-B10a 摘注册→2 红）。既有测试对齐新契约：test_p1_token_optimization 删 6 条死 L1 测试 + 1 条改写为热层命中契约、test_memory_upgrade 3 条 mock 改混合恢复方法、test_memory_sidecar 删死参、test_memory_vector_dim B10 声明改述。受影响 24 套件合跑 339 passed/13 既有基线失败；最终全量回归（无 shim 基线口径）52 failed/5425 passed，失败逐文件分布与既有 52 条基线完全一致（13+13+8+6+5+3+3+1），passed 恰为 P1 收尾 5414+新增闸门 17−删除死测试 6。闸门校准留痕 5 起（见勘误，均为闸门自身错误非产品缺陷）。
- 不算缺陷：CRM/ERP/邮件/OA connector 与开放平台多媒体接口为显式 501/ConnectorNotImplementedError，属已知未实现、行为诚实，不在本清单范围。
- 好消息备查：conflict_arbiter 与 forgetting 已真实接线（memory_manager.py:707、mem0_manager.py:93）；mem0/graphiti 为自研 PG 实现，无外部依赖坑。