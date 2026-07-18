"""
配置管理模块 — 单一职责：管理所有环境变量和配置项。

遵循开闭原则：新增配置项只需在 Settings 类中添加字段，无需修改其他代码。
遵循依赖倒置：所有模块通过依赖注入获取配置，不直接读取环境变量。
"""

from functools import lru_cache
from typing import Literal

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

    # === Redis ===
    REDIS_URL: str = "redis://localhost:6379/0"

    # === Milvus ===
    MILVUS_HOST: str = "localhost"
    MILVUS_PORT: int = 19530

    # === OpenSearch ===
    OPENSEARCH_URL: str = "http://localhost:9200"

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
    DEPLOY_MODE: Literal["saas", "private_overseas", "private_domestic"] = "saas"

    # === SaaS 模式 API Keys ===
    ANTHROPIC_API_KEY: str = ""
    OPENAI_API_KEY: str = ""
    COHERE_API_KEY: str = ""

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

    # === 文档解析增强 ===
    # PDF 表格提取 — pymupdf find_tables() → HTML <table>
    PDF_TABLE_EXTRACTION_ENABLED: bool = True
    # PDF 图片提取 + VLM 描述
    PDF_IMAGE_EXTRACTION_ENABLED: bool = True
    PDF_IMAGE_MAX_PER_DOC: int = 50
    # PDF 扫描页 OCR — get_text() 返回空时，页面渲染为图片调用 VLM 提取文字
    PDF_SCAN_OCR_ENABLED: bool = True
    PDF_SCAN_OCR_MAX_PAGES: int = 20  # 单个 PDF 最大 OCR 页数（防止大量扫描页打满 VLM）
    # PPTX 图片提取 + VLM 描述
    PPTX_IMAGE_EXTRACTION_ENABLED: bool = True
    PPTX_IMAGE_MAX_PER_DOC: int = 50
    # DOCX 表格提取 + 图片 VLM 描述
    DOCX_TABLE_EXTRACTION_ENABLED: bool = True
    DOCX_IMAGE_EXTRACTION_ENABLED: bool = True
    DOCX_IMAGE_MAX_PER_DOC: int = 50
    # XLSX 电子表格解析 — openpyxl 读取，每 sheet 转 HTML 表格
    XLSX_TABLE_EXTRACTION_ENABLED: bool = True
    XLSX_MAX_ROWS_PER_SHEET: int = 500  # 每个 sheet 最大提取行数
    XLSX_MAX_SHEETS: int = 20  # 单个文件最大提取 sheet 数

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

    # === Yjs 协作服务 ===
    YJS_WS_URL: str = "ws://localhost:8001"

    # === 记忆引擎 ===
    MEM0_CONFIG_PATH: str = "./config/mem0.yaml"

    # === LangFuse 可观测性 ===
    LANGFUSE_PUBLIC_KEY: str = ""
    LANGFUSE_SECRET_KEY: str = ""
    LANGFUSE_HOST: str = "https://cloud.langfuse.com"

    @property
    def is_saas(self) -> bool:
        """是否为 SaaS 模式。"""
        return self.DEPLOY_MODE == "saas"

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
