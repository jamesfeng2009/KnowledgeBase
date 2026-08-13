"""
配置管理模块 — 单一职责：管理所有环境变量和配置项。

遵循开闭原则：新增配置项只需在 Settings 类中添加字段，无需修改其他代码。
遵循依赖倒置：所有模块通过依赖注入获取配置，不直接读取环境变量。

Pydantic V2 校验策略：
    - 结构性校验（URL 格式、数值范围）→ 硬错误，启动即失败；
    - 运营性校验（API Key 缺失、默认密钥）→ warnings 警告，允许启动。
"""

import warnings
from functools import lru_cache
from typing import Literal
from urllib.parse import urlparse

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """应用配置 — 从环境变量加载，提供类型安全访问。"""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # === 应用基础 ===
    APP_NAME: str = "Enterprise Knowledge Brain"
    APP_VERSION: str = "0.1.0"
    DEBUG: bool = False
    SECRET_KEY: str = "change-me-in-production"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 1440  # 24h
    ALGORITHM: str = "HS256"

    # === 数据库 ===
    DATABASE_URL: str = "postgresql+asyncpg://ekb:ekb@localhost:5432/ekb"
    DATABASE_POOL_SIZE: int = 10
    DATABASE_MAX_OVERFLOW: int = 20
    # 启动时自动执行 Alembic 迁移（alembic upgrade head）
    # 开发/Demo 模式便捷开关；生产环境建议 CI/CD 中执行 alembic upgrade
    AUTO_MIGRATE: bool = True
    # 兼容旧逻辑 — 直接 create_all（不生成迁移文件，仅 demo 用）
    AUTO_CREATE_TABLES: bool = False

    # === Redis ===
    REDIS_URL: str = "redis://localhost:6379/0"

    # === Milvus ===
    MILVUS_HOST: str = "localhost"
    MILVUS_PORT: int = 19530

    # === OpenSearch ===
    OPENSEARCH_URL: str = "http://localhost:9200"
    # 全文 BM25 检索索引名 — 必须与写入方（tasks/document_tasks 的
    # _build_opensearch_index、tasks/index_tasks）使用同一索引，
    # 否则检索端查询不存在的索引会导致 BM25 被静默禁用。
    OPENSEARCH_INDEX: str = "ekb_documents"

    # C1/C2 fix: 跨模态图片向量使用独立索引，避免与文本向量维度冲突
    # SaaS 文本向量 3072 维 vs jina-clip-v2 图片向量 1024 维
    OPENSEARCH_CROSS_MODAL_INDEX: str = "ekb_cross_modal"

    # === 向量存储后端 ===
    # os_knn: OpenSearch k-NN（默认，< 500 万向量场景）
    # milvus: Milvus 向量引擎（可选，> 500 万向量场景）
    VECTOR_STORE: Literal["os_knn", "milvus"] = "os_knn"

    # === MinIO ===
    MINIO_ENDPOINT: str = "localhost:9000"
    MINIO_ACCESS_KEY: str = ""
    MINIO_SECRET_KEY: str = ""
    MINIO_BUCKET: str = "ekb-documents"

    # === Neo4j 知识图谱 ===
    NEO4J_URI: str = "bolt://localhost:7687"
    NEO4J_USER: str = "neo4j"
    NEO4J_PASSWORD: str = "ekb123456"
    NEO4J_DATABASE: str = "neo4j"

    # === 部署模式（核心切换变量）===
    # private_finetuned: 微调后的 7B + LoRA adapter，vLLM multi-LoRA 模式
    DEPLOY_MODE: Literal[
        "saas", "saas_dashscope",
        "private_overseas", "private_domestic", "private_finetuned",
    ] = "saas"

    # C6 fix: SaaS 模式默认关闭开放注册，需管理员邀请或审批
    REGISTRATION_ENABLED: bool = False

    # === SaaS 模式 API Keys ===
    ANTHROPIC_API_KEY: str = ""
    OPENAI_API_KEY: str = ""
    COHERE_API_KEY: str = ""

    # === SaaS 模式·国内（通义千问 DashScope）===
    # 阿里云通义千问，OpenAI 兼容接口，国内直连无需代理
    # Qwen-7B 无限制免费，qwen-turbo/qwen-plus 有新用户免费额度
    DASHSCOPE_API_KEY: str = ""
    DASHSCOPE_BASE_URL: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    DASHSCOPE_LLM_MODEL: str = "qwen-turbo"
    DASHSCOPE_EMBED_MODEL: str = "text-embedding-v3"
    DASHSCOPE_EMBED_DIM: int = 1024
    # DashScope 重排（gte-rerank，原生 HTTP API，非 OpenAI 兼容端点）
    DASHSCOPE_RERANK_MODEL: str = "gte-rerank-v2"
    # DashScope 视觉理解（qwen-vl 系列，走 OpenAI 兼容端点）
    DASHSCOPE_VLM_MODEL: str = "qwen-vl-max"

    # === 私有部署模型服务地址 ===
    VLLM_HOST: str = "llm-server"
    VLLM_PORT: str = "8003"
    VLLM_MODEL: str = "meta-llama/Llama-3.3-70B-Instruct"
    # private_finetuned 模式：微调后的 7B + LoRA adapter
    # vLLM multi-LoRA：model 字段传 adapter name（如 "dpo-v3-7b"）路由到 base+adapter
    VLLM_FINETUNED_MODEL: str = "Qwen2.5-7B-Instruct"
    VLLM_LORA_ADAPTER: str = "dpo-v3-7b"  # vLLM --lora-modules 注册的 adapter name
    VLLM_LORA_PATH: str = "/data/adapters/dpo-7b-v3"  # adapter 权重路径
    TEI_HOST: str = "embedding-server"
    TEI_PORT: str = "80"
    TEI_RERANKER_HOST: str = "reranker-server"
    TEI_RERANKER_PORT: str = "80"
    RERANKER_MODEL: str = "jinaai/jina-reranker-v2-base-multilingual"

    # === VLM ===
    VLM_HOST: str = "vlm-server"
    VLM_PORT: str = "8006"
    VLM_MODEL: str = "mistralai/Pixtral-12B-2409"

    # === ASR 语音转写 ===
    ASR_HOST: str = "asr-server"
    ASR_PORT: str = "8005"
    ASR_MODEL: str = "large-v3"

    # === 视频处理 ===
    VIDEO_KEYFRAME_ENABLED: bool = True
    VIDEO_KEYFRAME_SCENE_THRESHOLD: float = 0.3
    VIDEO_KEYFRAME_MAX_COUNT: int = 100
    # P2-C: 时长采样模式（替代场景检测全片扫描，突破 GB 级视频 OOM/磁盘打满）
    # 抽帧间隔（秒）— 每 N 秒抽取 1 帧（默认 5 分钟，2h 视频 ≈ 24 帧）
    VIDEO_KEYFRAME_INTERVAL: int = 300
    # 最大关键帧数 — 超出后均匀采样（默认 30，与 1GB/2h 视频量级匹配）
    VIDEO_KEYFRAME_MAX: int = 30
    # 黑屏/静态画面跳过阈值 — 帧直方图方差低于此值跳过（默认 100）
    VIDEO_KEYFRAME_VARIANCE_THRESHOLD: int = 100

    # === 文档解析增强 ===
    # PDF 表格提取 — pymupdf find_tables() → HTML <table>
    PDF_TABLE_EXTRACTION_ENABLED: bool = True
    # PDF 图片提取 + VLM 描述
    PDF_IMAGE_EXTRACTION_ENABLED: bool = True
    PDF_IMAGE_MAX_PER_DOC: int = 50
    # PDF 图片上传对象存储 — 启用后图片上传 MinIO 保留 URL（对齐图片流程），
    # 关闭时仅走 VLM 文本描述（当前模式）
    PDF_IMAGE_UPLOAD_ENABLED: bool = False
    # PDF 图片最小尺寸过滤 — 宽或高小于此值的图片跳过（剔除图标/装饰小图）
    PDF_IMAGE_MIN_SIZE: int = 50
    # PDF 扫描页 OCR — get_text() 返回空时，页面渲染为图片调用 VLM 提取文字
    PDF_SCAN_OCR_ENABLED: bool = True
    PDF_SCAN_OCR_MAX_PAGES: int = 20  # 单个 PDF 最大 OCR 页数（防止大量扫描页打满 VLM）
    # PDF 版式分层 — 基于 get_text("dict") 坐标重建阅读顺序（借鉴 pdfminer 栏检测），
    # 解决复杂多栏版式乱序；关闭或异常时降级为 get_text() 纯文本
    PDF_LAYOUT_ANALYSIS_ENABLED: bool = True
    # PPTX 图片提取 + VLM 描述
    PPTX_IMAGE_EXTRACTION_ENABLED: bool = True
    PPTX_IMAGE_MAX_PER_DOC: int = 50
    # PPTX 图片上传对象存储 + 小图过滤（对齐 PDF/DOCX）
    PPTX_IMAGE_UPLOAD_ENABLED: bool = False
    PPTX_IMAGE_MIN_SIZE: int = 50
    # DOCX 表格提取 + 图片 VLM 描述
    DOCX_TABLE_EXTRACTION_ENABLED: bool = True
    DOCX_IMAGE_EXTRACTION_ENABLED: bool = True
    DOCX_IMAGE_MAX_PER_DOC: int = 50
    # DOCX 图片上传对象存储 + 小图过滤（同 PDF）
    DOCX_IMAGE_UPLOAD_ENABLED: bool = False
    DOCX_IMAGE_MIN_SIZE: int = 50
    # DOCX 分页检测 — 检测 <w:br type="page"/> 和 <w:lastRenderedPageBreak/>
    # 启用后按实际页码分页，关闭时按段落序号排序（当前模式）
    DOCX_PAGE_BREAK_DETECTION: bool = True
    # XLSX 电子表格解析 — openpyxl 读取，每 sheet 转 HTML 表格
    XLSX_TABLE_EXTRACTION_ENABLED: bool = True
    XLSX_MAX_ROWS_PER_SHEET: int = 500  # 每个 sheet 最大提取行数
    XLSX_MAX_SHEETS: int = 20  # 单个文件最大提取 sheet 数

    # === 分页分隔 ===
    # 分页分隔符 — 非空时在页码变化处插入，支持 {page} 占位符
    # 示例："\n\n---\n<!-- page: {page} -->\n"
    # 默认空字符串（不分页标记，向后兼容）
    # 业界最佳实践：分页信息应进 chunk metadata，当前先用分隔符，未来演进
    PAGE_SEPARATOR: str = ""

    # === 独立音频解析 ===
    # 音频文件（mp3/wav/m4a 等）通过 ASR 转写为文本，复用视频 RAG 的分块管线
    AUDIO_ASR_ENABLED: bool = True

    # === Docling 统一文档解析（IBM Granite-Docling-258M，MIT 许可证）===
    # 启用后优先使用 Docling 解析 PDF/DOCX/PPTX/XLSX/HTML/图片/音频
    # Docling 不可用时自动降级到原有解析器（pymupdf/python-docx/openpyxl 等）
    DOCLING_ENABLED: bool = True
    # VLM 图片描述增强 — Docling 提取图片位置后用 VLM 生成描述注入 Markdown
    DOCLING_VLM_IMAGE_ENHANCE: bool = False

    # === MinerU 扫描件 OCR 增强 ===
    # 用 MinerU 替换 Docling 的 OCR 路径（仅扫描 PDF 与图片；Office/音频仍走 Docling）。
    # MinerU 运行在独立 venv（避免与 docling/mlx-vlm 依赖冲突），通过子进程调用。
    # 启用需配置 MINERU_PYTHON 指向 MinerU 独立 venv 的 python 绝对路径。
    MINERU_ENABLED: bool = False
    # MinerU 独立 venv 的 python 绝对路径（如 /path/to/mineru_test/venv/bin/python）
    MINERU_PYTHON: str = ""
    # 解析后端：pipeline / vlm-engine / hybrid-engine
    MINERU_BACKEND: str = "vlm-engine"
    # 文档语言（提升 OCR 精度，如 ch 中文 / en 英文）
    MINERU_LANG: str = "ch"
    # 单文件解析超时（秒）
    MINERU_TIMEOUT: int = 600

    # === Find Skills 渐进式技能加载 ===
    # 启用后 Agent Loop 先匹配相关技能再按需加载完整 schema，
    # 避免工具数量增长后全量加载浪费 token。
    SKILL_FINDER_ENABLED: bool = True
    # 匹配阈值 — 分数低于此值的技能不加载（name +10 / category +5 / tag +8 / desc +3）
    SKILL_MATCH_THRESHOLD: int = 5
    # 单次最多加载的技能数 — 防止过多工具淹没 LLM 上下文
    SKILL_MAX_LOADED: int = 10
    # P0-1: 向量召回通道 — 技能描述预计算 embedding，与关键词分数融合排序，
    # 补齐关键词语义盲区（"报销怎么走" → "费用审批流程"）；embedder 不可用时自动退化
    SKILL_VECTOR_RECALL_ENABLED: bool = True
    # 向量通道相似度阈值 — 低于此值不产生加分，防止语义噪声召回
    SKILL_VECTOR_SIM_THRESHOLD: float = 0.4
    # 向量通道权重 — 余弦相似度 × 此值折算加分（对齐 name 命中权重 +10）
    SKILL_VECTOR_WEIGHT: float = 10.0

    # === API 限流 ===
    RATE_LIMIT_ENABLED: bool = True
    RATE_LIMIT_PER_MINUTE: int = 60  # 每分钟请求数上限
    RATE_LIMIT_BURST: int = 10  # 突发请求数（令牌桶容量）

    # === RAG 质量守卫 ===
    # 检索参数（原硬编码常量，外置为配置）
    RAG_RETRIEVE_TOP_K: int = 20
    RAG_RERANK_TOP_K: int = 5
    RAG_MAX_ITERATIONS: int = 5
    # Agent Loop 超时分级（P1-7）— 单步骤超时与总任务超时分开管理
    AGENT_STEP_TIMEOUT_SECONDS: float = 60.0  # think/retrieve 单步骤超时
    AGENT_TOTAL_TIMEOUT_SECONDS: float = 300.0  # Agent Loop 决策循环总超时
    # 检索质量守卫 — 重排分数均值低于阈值时扩展 top_k 重排
    RAG_QUALITY_GUARD_ENABLED: bool = True
    RAG_RETRIEVAL_SCORE_THRESHOLD: float = 0.3
    RAG_RETRIEVAL_EXPAND_TOP_K: int = 10
    RAG_RETRIEVAL_MAX_RETRIES: int = 1
    # 生成质量守卫 — faithfulness 低于阈值时标记低置信度
    RAG_FAITHFULNESS_THRESHOLD: float = 3.0
    # P2: 动态匹配阈值 — 基于查询频率自适应调节
    # 高频查询（热门问题）阈值上浮（更严格筛选，减少噪声）；
    # 低频查询（冷门问题）阈值下浮（更宽松，避免漏召回）。
    RAG_DYNAMIC_THRESHOLD_ENABLED: bool = True
    RAG_THRESHOLD_FREQ_HOT_COUNT: int = 10  # 频次 >= 此值视为高频查询
    RAG_THRESHOLD_HOT_BOOST: float = 0.1  # 高频查询阈值上浮幅度
    RAG_THRESHOLD_COLD_DROP: float = 0.05  # 低频查询阈值下浮幅度
    RAG_THRESHOLD_FREQ_TTL: int = 86400  # 频次统计窗口（秒，默认 24h）
    RAG_THRESHOLD_MIN: float = 0.1  # 动态阈值下限
    RAG_THRESHOLD_MAX: float = 0.6  # 动态阈值上限

    # === 检索时间新鲜度（recency）===
    # 新旧规范冲突场景：分数相近时新版本优先 + 生效窗口硬过滤
    RECENCY_BOOST_ENABLED: bool = True  # 重排后对平局组按 updated_at 裁决
    RECENCY_TIE_BAND: float = 0.02  # 平局带宽：分差 <= 此值视为同分
    RECENCY_HALF_LIFE_DAYS: float = 180.0  # 新鲜度半衰期（天，用于可观测标注）

    # === P4 实时对话智能 ===
    # P4-A: 漂移检测
    DRIFT_DETECTION_ENABLED: bool = True
    DRIFT_SIMILARITY_THRESHOLD: float = 0.4  # cosine < 0.4 = 漂移
    DRIFT_POSSIBLE_THRESHOLD: float = 0.6  # 0.4-0.6 = 可能漂移
    # P4-B: 矛盾检测
    CONTRADICTION_DETECTION_ENABLED: bool = True
    CONTRADICTION_CHECK_USER_STATEMENTS: bool = True
    CONTRADICTION_CHECK_ANSWER_CONSISTENCY: bool = True
    CONTRADICTION_CHECK_DOC_CONTRADICTION: bool = False
    # P4-C: 指代消解增强
    COREFERENCE_INJECT_HISTORY: bool = True
    COREFERENCE_FOCUS_STACK_SIZE: int = 5
    # P4-D: 检索匹配检测
    RETRIEVAL_MATCH_CHECK_ENABLED: bool = True
    RETRIEVAL_MATCH_THRESHOLD: float = 0.3
    # P4-F: 偏好偏移检测
    PREFERENCE_DRIFT_ENABLED: bool = True
    # P4-G: 重复提问检测
    REPETITION_DETECTION_ENABLED: bool = True
    REPETITION_SIMILARITY_THRESHOLD: float = 0.85

    # === 离线评测 ===
    # 评测数据集目录（JSONL 格式）
    EVAL_DATASET_DIR: str = "./eval_datasets"
    # 回归阈值 — 指标下降超过此比例视为回归（如 0.05 = 下降 5%）
    EVAL_REGRESSION_THRESHOLD: float = 0.05

    # === LDAP ===
    LDAP_URL: str = ""
    LDAP_BIND_DN: str = ""
    LDAP_BIND_PASSWORD: str = ""
    LDAP_BASE_DN: str = ""

    # === OA 连接器（审批流查询 / IT 服务台对接）===
    # 关闭时 MCP query_oa_approval / create_it_ticket 工具走 Mock 适配器
    # （行为与历史 mock 一致）；启用且配置 API URL 后走真实 OA 系统
    CONNECTOR_OA_ENABLED: bool = False
    CONNECTOR_OA_API_URL: str = ""
    CONNECTOR_OA_API_KEY: str = ""

    # === CORS ===
    CORS_ORIGINS: list[str] = ["http://localhost:3000", "http://localhost:8090"]

    # === 文件上传限制 ===
    # 单文件上传大小上限（MB）— 超过返回 413 Payload Too Large
    # 对齐竞品 50MB 默认值，防止大文件打满内存
    MAX_UPLOAD_SIZE_MB: int = 50

    # === Yjs 协作服务 ===
    YJS_WS_URL: str = "ws://localhost:8001"

    # === 记忆引擎 ===
    MEM0_CONFIG_PATH: str = "./config/mem0.yaml"

    # === P2-1 副车道检索（Sidecar Memory Retrieval）===
    # 记忆召回（L3/L4）走独立"副车道"，不污染主对话 Prompt Cache 前缀。
    #   - MEMORY_SIDECAR_ENABLED：总开关，默认关闭（零行为回归）；
    #   - MEMORY_SIDECAR_MODEL：轻量模型 ID（models.json 中的 model_id），
    #     用于记忆召回的 LLM 辅助步骤（记忆查询改写）；留空则回退默认 Provider。
    MEMORY_SIDECAR_ENABLED: bool = False
    MEMORY_SIDECAR_MODEL: str = ""

    # === LangFuse 可观测性 ===
    LANGFUSE_PUBLIC_KEY: str = ""
    LANGFUSE_SECRET_KEY: str = ""
    LANGFUSE_HOST: str = "https://cloud.langfuse.com"

    # === P1-6 PII 脱敏（LangFuse span export 前应用）===
    # LangFuse 是外部观测系统，span input/output/metadata 会携带实际推理内容，
    # 其中可能含用户输入的 PII（手机/身份证/邮箱/银行卡），export 前必须脱敏。
    # 关闭后 span 原样上报（仅在内网自托管 LangFuse 且确认无 PII 风险时关闭）。
    LANGFUSE_PII_SCRUB_ENABLED: bool = True

    # === P2-9 LangFuse 采样策略（error span 强制不采样）===
    # 高流量场景下 LangFuse SaaS 配额易耗尽，且海量正常 span 淹没故障信号。
    # 采样策略（仅作用于 LangFuse 双写分支，本地 SpanRecord 不采样）：
    #   - 正常 span 按 LANGFUSE_SAMPLING_RATE 随机上报（控制成本与配额）
    #   - error span（metadata.error 非空）强制 100% 上报（保证故障可追溯）
    #   - 根 Span（task.run）强制不采样（保证每条 Trace 至少有一个锚点）
    # 默认关闭 — 避免改变现有行为；高流量上线时显式开启。
    LANGFUSE_SAMPLING_ENABLED: bool = False
    # 正常 span 采样率（0.0=全部丢弃，1.0=全部保留）。
    # 推荐 0.1（10% 采样，配 90% 成本节省，error span 100% 兜底）。
    LANGFUSE_SAMPLING_RATE: float = 0.1
    # error span 强制不采样的开关（独立于 LANGFUSE_SAMPLING_ENABLED，
    # 即使采样关闭，error span 仍可被此开关单独控制）。
    # 默认 True — error span 是故障排查的核心证据，不应被采样丢弃。
    LANGFUSE_SAMPLING_FORCE_ERROR: bool = True

    # === TTS 语音合成 ===
    TTS_VOICE: str = "zh-CN-XiaoxiaoNeural"  # 默认女声；男声 zh-CN-YunxiNeural
    TTS_RATE: str = "+0%"  # 语速调节：-50% 慢 ~ +50% 快
    TTS_VOLUME: str = "+0%"  # 音量调节：-50% ~ +50%
    TTS_ENABLED: bool = True  # TTS 总开关

    # === P2 跨模态向量检索 ===
    # 启用后文档中的图片直接向量化入库（无需仅依赖 VLM 文本描述），
    # 用户可用文本查询检索到图片内容（text-to-image cross-modal retrieval）。
    # 支持两种后端：
    #   - jina-clip-v2（海外，1024 维，批量 API）
    #   - DashScope tongyi-embedding-vision-flash（国内，1024 维，Flash 高性价比）
    # 根据 DEPLOY_MODE 自动选择：saas/private_overseas → Jina，saas_dashscope/private_domestic → DashScope
    CROSS_MODAL_ENABLED: bool = False
    JINA_API_KEY: str = ""  # Jina AI API Key（jina-clip-v2 免费额度）
    JINA_CLIP_MODEL: str = "jina-clip-v2"
    JINA_CLIP_DIM: int = 1024  # jina-clip-v2 输出维度
    # DashScope 通义多模态向量 Flash — 国内部署模式使用
    # P2: 从 multimodal-embedding-one-peace-v1 (1536维) 切换到 tongyi-embedding-vision-flash (1024维)
    # Flash 版本性价比更高，1024 维与 Jina 一致便于跨部署模式兼容
    DASHSCOPE_MULTIMODAL_MODEL: str = "tongyi-embedding-vision-flash"
    DASHSCOPE_MULTIMODAL_DIM: int = 1024  # tongyi-embedding-vision-flash 输出维度
    # 跨模态向量维度 — 根据 DEPLOY_MODE 自动选择
    # saas/private_overseas → JINA_CLIP_DIM (1024)
    # saas_dashscope/private_domestic → DASHSCOPE_MULTIMODAL_DIM (1536)
    CROSS_MODAL_DIM: int = 1024  # 默认 Jina 维度，DashScope 模式设为 1536

    # === P1-A 优雅关闭 ===
    SHUTDOWN_TIMEOUT: int = 30  # 优雅关闭超时（秒），uvicorn --timeout-graceful-shutdown
    SHUTDOWN_GRACE_PERIOD_CORE: int = 30  # core-engine Docker stop_grace_period
    SHUTDOWN_GRACE_PERIOD_CELERY: int = 60  # celery-worker Docker stop_grace_period

    # === P1-A 指数退避重试（三层体系共享参数）===
    RETRY_BACKOFF_BASE: float = 1.0  # 基础延迟（秒），HTTP 调用
    RETRY_BACKOFF_BASE_CELERY: float = 5.0  # Celery 任务基础延迟（秒）
    RETRY_BACKOFF_BASE_DB: float = 0.5  # 数据库操作基础延迟（秒）
    RETRY_BACKOFF_MAX: float = 60.0  # 最大延迟上限（秒）
    RETRY_MAX_ATTEMPTS: int = 3  # 最大重试次数
    RETRY_JITTER: float = 1.0  # 抖动范围（秒），全抖动模式

    # === P0-2 LLM Provider 重试（专用参数，比普通 HTTP 更保守）===
    # LLM 调用昂贵且限流常见，重试策略需独立配置：
    #   - 更多重试次数（限流恢复需要时间）
    #   - 更长基础延迟（避免连续打满限流窗口）
    #   - 更短最大延迟（避免用户单次等待超 30 秒）
    LLM_RETRY_MAX_ATTEMPTS: int = 4  # LLM 最大重试次数（含首次，比普通 HTTP 多 1 次）
    LLM_RETRY_BACKOFF_BASE: float = 2.0  # LLM 基础延迟（秒）
    LLM_RETRY_BACKOFF_MAX: float = 32.0  # LLM 最大延迟上限（秒）

    # === P1-4 检索结果注入扫描（RAG 安全核心）===
    # 启用后 HybridRetriever 返回的候选文档经 InjectionGuard 扫描，
    # 命中 prompt injection 模式的文档进 quarantined 列表（不丢弃，保留审计证据）。
    # 6 类注入模式：指令劫持 / 角色覆盖 / 数据外泄 / 工具滥用 / 越狱 / 分隔符注入
    RAG_INJECTION_GUARD_ENABLED: bool = True
    # 隔离阈值 — 严重级别 >= 此值的命中才隔离（low/medium/high）
    #   "low"    ：所有命中隔离（最严格，高敏感场景）
    #   "medium" ：MEDIUM + HIGH 隔离（默认，平衡误报与安全）
    #   "high"   ：仅 HIGH 隔离（最宽松，低风险场景）
    RAG_INJECTION_GUARD_QUARANTINE_THRESHOLD: str = "medium"
    # 命中后是否触发 admin 告警（通过 NotificationService 发送站内通知）
    RAG_INJECTION_GUARD_ALERT_ADMINS: bool = True

    # === P0-3 四轴硬预算（Agent Loop 前置闸门）===
    # 单次 answer() 调用的四轴硬上限，任一触顶即抛 BudgetExceeded 硬停。
    # 与 max_iterations / AGENT_TOTAL_TIMEOUT_SECONDS 并存：
    #   - max_iterations: 软终止（break 循环，走兜底生成）
    #   - 硬预算: 硬终止（抛异常，拒绝继续）
    # 保守默认：无人值守的 run 应"宁可早死，不要烧钱"。
    AGENT_BUDGET_MAX_TURNS: int = 10  # 决策循环最大迭代次数（硬上限，软 max_iterations 之上）
    AGENT_BUDGET_MAX_SECONDS: float = 300.0  # wall-clock 最大时间（秒，与 AGENT_TOTAL_TIMEOUT_SECONDS 对齐）
    AGENT_BUDGET_MAX_TOKENS: int = 200_000  # 累积 token 上限（cache-read 从基数扣除）
    AGENT_BUDGET_MAX_COST_USD: float = 1.0  # 累积成本上限（美元）

    # === P1-A 熔断器 ===
    CIRCUIT_BREAKER_FAILURE_THRESHOLD: int = 5  # 滑动窗口内失败次数触发熔断
    CIRCUIT_BREAKER_RECOVERY_TIMEOUT: float = 30.0  # OPEN → HALF_OPEN 冷却时间（秒）
    CIRCUIT_BREAKER_HALF_OPEN_MAX_CALLS: int = 1  # 半开状态最多探测请求数
    # P2-8: 滑动窗口大小（秒）— 只统计窗口内的失败时间戳，
    # 过期时间戳自动从 deque 左侧淘汰。窗口外失败不计数，
    # 避免历史偶发失败永久累计导致服务"无法恢复"。
    # 与 CIRCUIT_BREAKER_FAILURE_THRESHOLD 组合：
    #   "窗口内失败 >= threshold 才熔断"
    CIRCUIT_BREAKER_FAILURE_WINDOW: float = 60.0

    # === P1-B 幂等锁 ===
    TASK_LOCK_TTL: int = 1800  # Celery 任务幂等锁 TTL（秒），默认 30 分钟
    TASK_LOCK_REDIS_PREFIX: str = "lock:task:"  # 任务锁 Redis key 前缀

    # === P1-B 内容哈希去重 ===
    DEDUP_SCOPE_KB_ONLY: bool = True  # 查重范围仅限同 KB 内（False=全局查重）

    # === P2-A Provider 健康检查 + 故障转移 ===
    HEALTH_CHECK_INTERVAL: int = 30  # 健康检查间隔（秒）
    HEALTH_CHECK_CACHE_TTL: int = 60  # 健康检查结果 Redis 缓存 TTL（秒）
    LLM_FAILOVER_CHAIN: str = ""  # LLM 故障转移链 "dashscope,vllm"
    EMBEDDER_FAILOVER_CHAIN: str = ""  # Embedder 故障转移链 "openai,tei"
    RERANKER_FAILOVER_CHAIN: str = ""  # Reranker 故障转移链 "cohere,tei"
    VECTOR_STORE_FAILOVER_CHAIN: str = ""  # VectorStore 故障转移链 "opensearch,milvus"

    # === P2-B 查询重写/扩展 ===
    QUERY_REWRITE_ENABLED: bool = True  # 查询重写（修正拼写/消歧）
    QUERY_EXPANSION_ENABLED: bool = True  # 查询扩展（同义词/相关词）
    QUERY_DECOMPOSITION_ENABLED: bool = False  # 查询分解（复杂查询拆子查询）
    HYDE_ENABLED: bool = False  # HyDE 假设文档生成
    # === P1-10 改写策略自动路由 + 在线回退 ===
    QUERY_REWRITE_AUTO_ROUTE: bool = False  # 按 query 类型自动路由策略（规则分类，零 LLM）
    QUERY_REWRITE_ONLINE_FALLBACK: bool = False  # 改写后双跑对比召回，更差则回退原 query
    QUERY_REWRITE_FALLBACK_MARGIN: float = 0.0  # 回退判定余量（防噪声抖动）
    # === P2-12 请求队列 + 配置化降级 ===
    RATE_LIMIT_QUEUE_ENABLED: bool = True  # 429 前短队列缓冲（返回预计等待时间）
    RATE_LIMIT_QUEUE_MAX_WAIT_MS: int = 2000  # 允许排队的最大预计等待毫秒数
    RATE_LIMIT_QUEUE_MAX_QUEUED: int = 20  # 全局同时排队请求数上限
    DEGRADE_MODE_ENABLED: bool = False  # 降级模式：高负载时关闭 HyDE/QueryDecomposition 保核心链路
    # === P2-13 长任务里程碑 + 超时分级 ===
    TASK_STEP_TIMEOUT_SECONDS: int = 300  # Celery 长任务单阶段超时（总超时由 task_time_limit 控制）

    # === P3 缓存容量上限 ===
    CACHE_L2_MAX_SIZE: int = 1000  # L2 语义缓存（进程内存）最大条目数，超容逐出最旧（LRU）

    # === P1 IntentRouter 稳态/敏态分离 ===
    INTENT_ROUTER_ENABLED: bool = True          # IntentRouter 总开关
    INTENT_ROUTER_LLM_FALLBACK: bool = True     # 规则未命中时是否调 LLM 解析意图
    INTENT_ROUTER_CONFIDENCE_THRESHOLD: float = 0.7  # LLM 意图置信度阈值
    INTENT_SHORTCUT_ENABLED: bool = True        # 是否启用快捷路径（False=全部走 Agent Loop）

    # === 聊天问答 → 知识库回流（P0）===
    # 好评反馈 / 采纳答案自动沉淀为 KB FAQ 文档，复用 KnowledgeCompoundingService 5 步框架。
    # 详见 app/services/knowledge_compounding/compounding_service.py
    #   extract_from_chat_feedback / extract_from_accepted_answer
    CHAT_FAQ_COMPOUNDING_ENABLED: bool = True   # 总开关（False=不触发任何回流）
    # 全局唯一 FAQ 知识库 ID（UUID 字符串）。空字符串表示未配置，回流任务将跳过并告警。
    FAQ_KB_ID: str = ""
    # 好评阈值：Feedback.type → 分值映射（praise=5/suggestion=3/complaint=2/bug=1），
    # 分值 >= 此值才触发回流。默认 4 = 仅 praise 触发（与 dataset_builder FEEDBACK_RATING 口径一致）。
    CHAT_FAQ_MIN_PRAISE_RATING: int = 4
    # 单条 FAQ 内容最大字符数（防超长内容撑爆检索窗口）
    CHAT_FAQ_MAX_CONTENT_CHARS: int = 4000
    # P2 审批自动通过阈值 — quality_score >= 此值 且 无冲突 且 无 PII 时自动 approve。
    # 默认 0.9（保守，优先人工审批）；设为 1.0 则全部人工审批。
    CHAT_FAQ_AUTO_APPROVE_THRESHOLD: float = 0.9
    # P2 审批过期时间（秒）— pending 状态超时后标记 expired。默认 7 天。
    CHAT_FAQ_APPROVAL_TTL_SECONDS: int = 7 * 24 * 3600

    # === P2 EntityRegistry 企业本体 ===
    ENTITY_REGISTRY_ENABLED: bool = True        # EntityRegistry 总开关
    GRAPH_SEARCH_ENABLED: bool = True           # 图谱召回开关（HybridRetriever 第四路）
    GRAPH_SEARCH_MAX_DEPTH: int = 2             # 图谱遍历最大跳数
    GRAPH_SEARCH_MAX_RESULTS: int = 10          # 图谱召回最大结果数
    GRAPH_SEARCH_SCORE: float = 0.5             # 图谱召回固定分（合并时的权重）

    # === 知识推荐（Recommendation）===
    # 三路召回按数据成熟度分级启停（冷启动 → 行为积累），非默认全开。
    # 每路一个开关；协同过滤在用户行为数达阈值后才启用（冷启动回退向量+热门）。
    RECOMMEND_ENABLED: bool = True            # 推荐模块总开关
    RECOMMEND_ENABLE_CF: bool = True          # 协同过滤召回开关（需行为积累）
    RECOMMEND_ENABLE_VECTOR: bool = True      # 向量内容召回开关（冷启动兜底）
    RECOMMEND_ENABLE_GRAPH: bool = True       # 图谱关联召回开关
    RECOMMEND_CF_MIN_INTERACTIONS: int = 3    # 用户行为数达此阈值才启用协同过滤
    RECOMMEND_CF_SIMILAR_USERS: int = 10      # UserCF 相似用户数
    RECOMMEND_CF_SIMILAR_ITEMS: int = 10      # ItemCF 相似文档数
    RECOMMEND_RRF_K: int = 60                 # RRF 常数（1/(K+rank)）
    RECOMMEND_HOT_FALLBACK_TOP_K: int = 10    # 冷启动热门兜底数量
    RECOMMEND_CACHE_TTL: int = 300            # 推荐结果缓存 TTL（秒）

    # === P3 上下文工程 ===
    CONTEXT_FOCUS_TRACKING_ENABLED: bool = True       # P3-A 焦点追踪总开关
    CONTEXT_FOCUS_HISTORY_WINDOW: int = 12            # 焦点追踪加载的历史消息数
    COREFERENCE_RESOLUTION_ENABLED: bool = True       # P3-A 指代消解开关
    CONTEXT_SELECTOR_ENABLED: bool = True             # P3-B 语义选择器开关
    CONTEXT_SELECTOR_TOP_K: int = 5                   # 语义选择器最多选中消息数
    CONTEXT_SELECTOR_MAX_TOKENS: int = 800            # 语义选择器 token 预算
    CONVERSATION_SUMMARIZER_ENABLED: bool = True      # P3-C 滚动摘要开关
    CONVERSATION_SUMMARIZER_MAX_TOKENS: int = 600     # 摘要触发阈值
    CONVERSATION_SUMMARIZER_RETAINED_TOKENS: int = 200  # 摘要后保留的近期 token
    DETAIL_RECALL_ENABLED: bool = True                # P1-3 历史细节召回开关
    DETAIL_RECALL_LIMIT: int = 3                      # 细节召回条数上限
    DETAIL_RECALL_MAX_TOKENS: int = 300               # 细节召回注入 token 上限
    SCRATCHPAD_ENABLED: bool = True                   # P3-E Scratchpad 开关
    LLM_FACT_EXTRACTION_ENABLED: bool = True          # P3-F LLM 事实提取开关

    # === 外部文档同步巡检（P2 定时兜底）===
    # 方案 A：单一阈值，所有外部文档无类别区分，无盲区
    # last_checked_at 天然限流 — P0/P1 已校验的文档不会重复巡检
    EXTERNAL_SYNC_PATROL_ENABLED: bool = True           # 巡检总开关
    EXTERNAL_SYNC_PATROL_MAX_STALENESS_HOURS: int = 24  # 最大滞后阈值（h）
    EXTERNAL_SYNC_PATROL_BATCH_SIZE: int = 50           # 单批巡检文档数上限
    EXTERNAL_SYNC_PATROL_CONCURRENCY: int = 2            # 并发上限（防 IP 封禁）

    # ================================================================
    # Pydantic V2 校验器 — 结构性校验硬失败，运营性校验发 warning
    # ================================================================

    @field_validator("DATABASE_URL")
    @classmethod
    def validate_database_url(cls, v: str) -> str:
        """DATABASE_URL 必须使用 PostgreSQL 异步驱动，否则 SQLAlchemy 异步引擎无法启动。"""
        if not v.startswith("postgresql+asyncpg://"):
            raise ValueError(
                f"DATABASE_URL 必须使用 postgresql+asyncpg:// 异步驱动，"
                f"当前值: {v}"
            )
        return v

    @field_validator(
        "DATABASE_POOL_SIZE",
        "DATABASE_MAX_OVERFLOW",
        "RATE_LIMIT_PER_MINUTE",
        "RATE_LIMIT_BURST",
        "SKILL_MAX_LOADED",
        "ACCESS_TOKEN_EXPIRE_MINUTES",
        "RAG_RETRIEVE_TOP_K",
        "RAG_RERANK_TOP_K",
        "RAG_MAX_ITERATIONS",
        "RAG_RETRIEVAL_EXPAND_TOP_K",
        "MAX_UPLOAD_SIZE_MB",
        "VIDEO_KEYFRAME_INTERVAL",
        "VIDEO_KEYFRAME_MAX",
        "SHUTDOWN_TIMEOUT",
        "SHUTDOWN_GRACE_PERIOD_CORE",
        "SHUTDOWN_GRACE_PERIOD_CELERY",
        "RETRY_MAX_ATTEMPTS",
        "LLM_RETRY_MAX_ATTEMPTS",
        "AGENT_BUDGET_MAX_TURNS",
        "AGENT_BUDGET_MAX_TOKENS",
        "CIRCUIT_BREAKER_FAILURE_THRESHOLD",
        "CIRCUIT_BREAKER_HALF_OPEN_MAX_CALLS",
        "CIRCUIT_BREAKER_FAILURE_WINDOW",
        "TASK_LOCK_TTL",
        "GRAPH_SEARCH_MAX_DEPTH",
        "GRAPH_SEARCH_MAX_RESULTS",
        "CONTEXT_FOCUS_HISTORY_WINDOW",
        "CONTEXT_SELECTOR_TOP_K",
        "CONTEXT_SELECTOR_MAX_TOKENS",
        "CONVERSATION_SUMMARIZER_MAX_TOKENS",
        "CONVERSATION_SUMMARIZER_RETAINED_TOKENS",
        "RAG_THRESHOLD_FREQ_HOT_COUNT",
        "RAG_THRESHOLD_FREQ_TTL",
    )
    @classmethod
    def validate_positive_int(cls, v: int) -> int:
        """数值配置必须为正整数。"""
        if v <= 0:
            raise ValueError(f"值必须为正整数，当前: {v}")
        return v

    @field_validator(
        "RETRY_BACKOFF_BASE",
        "RETRY_BACKOFF_BASE_CELERY",
        "RETRY_BACKOFF_BASE_DB",
        "RETRY_BACKOFF_MAX",
        "RETRY_JITTER",
        "LLM_RETRY_BACKOFF_BASE",
        "LLM_RETRY_BACKOFF_MAX",
        "AGENT_BUDGET_MAX_SECONDS",
        "AGENT_BUDGET_MAX_COST_USD",
        "CIRCUIT_BREAKER_RECOVERY_TIMEOUT",
        "CIRCUIT_BREAKER_FAILURE_WINDOW",
        "RAG_THRESHOLD_HOT_BOOST",
        "RAG_THRESHOLD_COLD_DROP",
        "AGENT_STEP_TIMEOUT_SECONDS",
        "AGENT_TOTAL_TIMEOUT_SECONDS",
    )
    @classmethod
    def validate_positive_float(cls, v: float) -> float:
        """浮点数配置必须为正数。"""
        if v <= 0:
            raise ValueError(f"值必须为正数，当前: {v}")
        return v

    @field_validator(
        "SKILL_MATCH_THRESHOLD",
        "RAG_RETRIEVAL_MAX_RETRIES",
        "PDF_IMAGE_MIN_SIZE",
        "DOCX_IMAGE_MIN_SIZE",
        "PPTX_IMAGE_MIN_SIZE",
        "VIDEO_KEYFRAME_VARIANCE_THRESHOLD",
    )
    @classmethod
    def validate_non_negative_int(cls, v: int) -> int:
        """阈值/重试次数/最小尺寸必须为非负整数。"""
        if v < 0:
            raise ValueError(f"值必须为非负整数，当前: {v}")
        return v

    @field_validator(
        "RAG_RETRIEVAL_SCORE_THRESHOLD",
        "EVAL_REGRESSION_THRESHOLD",
        "VIDEO_KEYFRAME_SCENE_THRESHOLD",
        "INTENT_ROUTER_CONFIDENCE_THRESHOLD",
        "GRAPH_SEARCH_SCORE",
        "RAG_THRESHOLD_MIN",
        "RAG_THRESHOLD_MAX",
        "LANGFUSE_SAMPLING_RATE",
    )
    @classmethod
    def validate_float_0_1(cls, v: float) -> float:
        """浮点阈值必须在 [0, 1] 范围内。"""
        if not 0.0 <= v <= 1.0:
            raise ValueError(f"值必须在 [0.0, 1.0] 范围内，当前: {v}")
        return v

    @field_validator("CORS_ORIGINS")
    @classmethod
    def validate_cors_origins(cls, v: list[str]) -> list[str]:
        """CORS 来源必须是合法 URL（含 scheme 和 host）。"""
        for origin in v:
            parsed = urlparse(origin)
            if not parsed.scheme or not parsed.netloc:
                raise ValueError(
                    f"CORS 来源必须是合法 URL (如 http://localhost:3000)，当前: {origin}"
                )
        return v

    @model_validator(mode="after")
    def validate_deploy_mode_keys(self) -> "Settings":
        """根据部署模式校验必要的 API Key — 仅警告，不阻断启动。"""
        if self.DEPLOY_MODE == "saas_dashscope" and not self.DASHSCOPE_API_KEY:
            warnings.warn(
                "DEPLOY_MODE=saas_dashscope 但未设置 DASHSCOPE_API_KEY，"
                "LLM 调用将在运行时失败",
                stacklevel=2,
            )
        if self.DEPLOY_MODE == "saas" and not any(
            [self.ANTHROPIC_API_KEY, self.OPENAI_API_KEY, self.COHERE_API_KEY]
        ):
            warnings.warn(
                "DEPLOY_MODE=saas 但未设置任何 LLM API Key "
                "(ANTHROPIC_API_KEY / OPENAI_API_KEY / COHERE_API_KEY)",
                stacklevel=2,
            )
        # P2: 跨模态向量维度自动匹配部署模式
        if self.CROSS_MODAL_ENABLED:
            if self.DEPLOY_MODE in ("saas_dashscope", "private_domestic"):
                self.CROSS_MODAL_DIM = self.DASHSCOPE_MULTIMODAL_DIM
            else:
                self.CROSS_MODAL_DIM = self.JINA_CLIP_DIM
        return self

    @model_validator(mode="after")
    def validate_secret_key(self) -> "Settings":
        """生产环境 (DEBUG=False) 不允许使用默认 SECRET_KEY — 仅警告。"""
        if self.SECRET_KEY == "change-me-in-production" and not self.DEBUG:
            warnings.warn(
                "生产环境 (DEBUG=False) 使用默认 SECRET_KEY 极不安全，"
                "请立即通过环境变量设置随机密钥",
                stacklevel=2,
            )
        return self

    # ================================================================
    # 便捷属性
    # ================================================================

    @property
    def is_saas(self) -> bool:
        """是否为 SaaS 模式（含 Anthropic 和 DashScope）。"""
        return self.DEPLOY_MODE in ("saas", "saas_dashscope")

    @property
    def is_private(self) -> bool:
        """是否为私有部署模式。"""
        return self.DEPLOY_MODE in (
            "private_overseas", "private_domestic", "private_finetuned",
        )

    @property
    def vllm_base_url(self) -> str:
        """vLLM 服务 OpenAI 兼容 API 地址。"""
        return f"http://{self.VLLM_HOST}:{self.VLLM_PORT}/v1"

    @property
    def tei_embed_url(self) -> str:
        """TEI Embedding 服务地址。"""
        return f"http://{self.TEI_HOST}:{self.TEI_PORT}"

    @property
    def tei_reranker_url(self) -> str:
        """TEI Reranker 服务地址。"""
        return f"http://{self.TEI_RERANKER_HOST}:{self.TEI_RERANKER_PORT}"


@lru_cache
def get_settings() -> Settings:
    """单例模式获取配置实例 — 缓存避免重复解析环境变量。"""
    return Settings()
