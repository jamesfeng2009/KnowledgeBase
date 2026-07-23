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
    DEPLOY_MODE: Literal[
        "saas", "saas_dashscope", "private_overseas", "private_domestic"
    ] = "saas"

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

    # === 私有部署模型服务地址 ===
    VLLM_HOST: str = "llm-server"
    VLLM_PORT: str = "8003"
    VLLM_MODEL: str = "meta-llama/Llama-3.3-70B-Instruct"
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

    # === Find Skills 渐进式技能加载 ===
    # 启用后 Agent Loop 先匹配相关技能再按需加载完整 schema，
    # 避免工具数量增长后全量加载浪费 token。
    SKILL_FINDER_ENABLED: bool = True
    # 匹配阈值 — 分数低于此值的技能不加载（name +10 / category +5 / tag +8 / desc +3）
    SKILL_MATCH_THRESHOLD: int = 5
    # 单次最多加载的技能数 — 防止过多工具淹没 LLM 上下文
    SKILL_MAX_LOADED: int = 10

    # === API 限流 ===
    RATE_LIMIT_ENABLED: bool = True
    RATE_LIMIT_PER_MINUTE: int = 60  # 每分钟请求数上限
    RATE_LIMIT_BURST: int = 10  # 突发请求数（令牌桶容量）

    # === RAG 质量守卫 ===
    # 检索参数（原硬编码常量，外置为配置）
    RAG_RETRIEVE_TOP_K: int = 20
    RAG_RERANK_TOP_K: int = 5
    RAG_MAX_ITERATIONS: int = 5
    # 检索质量守卫 — 重排分数均值低于阈值时扩展 top_k 重排
    RAG_QUALITY_GUARD_ENABLED: bool = True
    RAG_RETRIEVAL_SCORE_THRESHOLD: float = 0.3
    RAG_RETRIEVAL_EXPAND_TOP_K: int = 10
    RAG_RETRIEVAL_MAX_RETRIES: int = 1
    # 生成质量守卫 — faithfulness 低于阈值时标记低置信度
    RAG_FAITHFULNESS_THRESHOLD: float = 3.0

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

    # === LangFuse 可观测性 ===
    LANGFUSE_PUBLIC_KEY: str = ""
    LANGFUSE_SECRET_KEY: str = ""
    LANGFUSE_HOST: str = "https://cloud.langfuse.com"

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

    # === P1-A 熔断器 ===
    CIRCUIT_BREAKER_FAILURE_THRESHOLD: int = 5  # 连续失败次数触发熔断
    CIRCUIT_BREAKER_RECOVERY_TIMEOUT: float = 30.0  # OPEN → HALF_OPEN 冷却时间（秒）
    CIRCUIT_BREAKER_HALF_OPEN_MAX_CALLS: int = 1  # 半开状态最多探测请求数

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

    # === P3 缓存容量上限 ===
    CACHE_L2_MAX_SIZE: int = 1000  # L2 语义缓存（进程内存）最大条目数，超容逐出最旧（LRU）

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
        "CIRCUIT_BREAKER_FAILURE_THRESHOLD",
        "CIRCUIT_BREAKER_HALF_OPEN_MAX_CALLS",
        "TASK_LOCK_TTL",
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
        "CIRCUIT_BREAKER_RECOVERY_TIMEOUT",
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
        return self.DEPLOY_MODE in ("private_overseas", "private_domestic")

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
