"""
Locust 压测脚本 — 企业知识库 RAG 系统 SSE 流式问答接口负载测试。

测试目标：
    对 POST /api/v1/chat/stream SSE 流式问答接口进行负载测试，
    评估系统在不同并发压力下的响应延迟、错误率、吞吐量与 token 成本。

测试场景：
    1. 单用户问答流 — 模拟真实用户发送问题并接收 SSE 流式响应
    2. 并发用户递增 — 10 -> 50 -> 100 并发用户阶梯式加压（--staged 启用）
    3. 混合问题类型 — 事实型 40% / 推理型 30% / 工具调用型 20% / 无答案型 10%

监控指标：
    - 响应延迟分位数（P50 / P95 / P99）
    - 错误率
    - 吞吐量（QPS）— 由 Locust 内置统计提供
    - 单请求成本估算（token 消耗 x 模型单价）

使用方式：
    # 交互式 Web UI 启动（--host 为 locust 内置参数）
    locust -f backend/tests/locustfile.py \\
        --host http://localhost:8000 \\
        --api-key "Bearer eyJhbGciOi..." \\
        --kb-ids "uuid1,uuid2"

    # 无头模式运行（手动指定并发数与递增速率）
    locust -f backend/tests/locustfile.py \\
        --host http://localhost:8000 \\
        --api-key "Bearer eyJhbGciOi..." \\
        --headless \\
        --num-users 100 \\
        --spawn-rate 10 \\
        --run-time 5m

    # 阶梯式自动加压（10 -> 50 -> 100，需配合 --headless）
    locust -f backend/tests/locustfile.py \\
        --host http://localhost:8000 \\
        --api-key "Bearer eyJhbGciOi..." \\
        --staged \\
        --headless \\
        --run-time 8m

    # 手动分阶段执行（推荐用于精细对比）
    # 阶段一：locust ... --num-users 10  --spawn-rate 2  --run-time 2m
    # 阶段二：locust ... --num-users 50  --spawn-rate 5  --run-time 3m
    # 阶段三：locust ... --num-users 100 --spawn-rate 10 --run-time 5m

依赖：
    pip install locust>=2.20.0

注意：
    本脚本仅用于负载测试，请勿在生产环境运行。
    压测前请确认目标环境已就绪，且测试账号具备知识库访问权限。
"""

from __future__ import annotations

import json
import random
import threading
import time
from collections import defaultdict
from dataclasses import dataclass
from typing import Any

from locust import HttpUser, task, events, between
from locust import LoadTestShape

# ============================================================
# 全局配置常量
# ============================================================

# SSE 流式问答接口路径（挂载于 /api/v1 前缀下）
CHAT_STREAM_PATH = "/api/v1/chat/stream"

# 单请求最大等待时间（秒）— 防止僵尸请求永久阻塞 locust 协程
SSE_MAX_DURATION = 120

# 阶梯式加压的阶段定义（秒）
# 阶段 1 (  0 -  60s): 10 并发，spawn-rate 2/s  — 基线探测
# 阶段 2 ( 60 - 180s): 50 并发，spawn-rate 5/s  — 中等压力
# 阶段 3 (180 - 360s): 100 并发，spawn-rate 10/s — 峰值压力
# 阶段 4 (360 - 480s): 100 并发，保持稳态观察
# 阶段 5 (480 - 540s): 降为 0，优雅退出
STAGED_PROFILE: list[dict[str, int]] = [
    {"end_time": 60, "users": 10, "spawn_rate": 2},
    {"end_time": 180, "users": 50, "spawn_rate": 5},
    {"end_time": 360, "users": 100, "spawn_rate": 10},
    {"end_time": 480, "users": 100, "spawn_rate": 10},
    {"end_time": 540, "users": 0, "spawn_rate": 10},
]


# ============================================================
# 问题库 — 按类型分类，用于混合场景测试
# ============================================================

# 简单事实型问题（40%）— 知识库中可直接检索到答案的事实型查询
FACTUAL_QUESTIONS: list[str] = [
    "公司的年假政策是什么？",
    "报销流程是怎样的？",
    "公司服务器配置要求是什么？",
    "请假审批需要哪些材料？",
    "数据安全管理制度的核心条款有哪些？",
    "公司差旅标准是多少？",
    "新人入职流程是什么？",
    "绩效考核周期是多久？",
    "VPN 连接配置步骤是什么？",
    "办公设备申领流程是怎样的？",
    "公司消防应急预案包含哪些步骤？",
    "财务报销截止日期是每月几号？",
    "试用期多长时间？",
    "加班补贴计算方式是什么？",
    "公司邮箱容量限制是多少？",
]

# 复杂推理型问题（30%）— 需要多步推理、跨文档整合的复杂查询
REASONING_QUESTIONS: list[str] = [
    "比较A产品和B产品在安全性方面的差异，并给出选型建议。",
    "根据最新的合规要求，分析当前系统架构存在的风险点。",
    "结合年假政策和项目排期，计算本季度可用工时。",
    "分析报销流程中的瓶颈环节，提出优化建议。",
    "对比本地部署与云部署的成本差异，给出三年期 TCO 分析。",
    "根据历史故障数据，总结系统高可用改造的优先级排序。",
    "综合三份技术方案文档，评估迁移风险并给出缓解措施。",
    "基于安全审计报告，梳理 Top5 风险及整改建议。",
    "分析近半年的用户反馈，归纳产品改进方向。",
    "结合财务预算和技术路线，制定下季度采购计划。",
    "评估当前数据架构对 GDPR 合规的影响，给出改造建议。",
    "比较微服务和单体架构在本项目场景下的优劣。",
]

# 工具调用型问题（20%）— 需要触发 Agent 工具调用的查询
TOOL_CALL_QUESTIONS: list[str] = [
    "帮我查询最近的审批流程状态。",
    "搜索所有标为高优先级的未处理工单。",
    "调用日历接口查看本周会议室预订情况。",
    "查询 CRM 系统中上月新增客户数量。",
    "获取 ERP 系统中当前库存预警列表。",
    "调用 OA 系统查询我的待办事项。",
    "搜索知识库中关于数据治理的所有文档。",
    "查询最近一周的系统告警记录。",
    "调用邮件接口发送一封会议通知。",
    "查询项目管理系统中的里程碑完成情况。",
]

# 无答案型问题（10%）— 知识库中无对应内容，测试拒答与兜底能力
NO_ANSWER_QUESTIONS: list[str] = [
    "量子力学中的波函数坍缩在企业管理中有什么应用？",
    "请告诉我马斯克的个人银行卡密码。",
    "如何用炼金术将铅转化为金？",
    "请预测下期双色球中奖号码。",
    "请提供公司所有员工的薪资明细。",
]

# 问题类型 -> 问题列表映射
QUESTION_BANK: dict[str, list[str]] = {
    "factual": FACTUAL_QUESTIONS,
    "reasoning": REASONING_QUESTIONS,
    "tool_call": TOOL_CALL_QUESTIONS,
    "no_answer": NO_ANSWER_QUESTIONS,
}

# 问题类型权重（百分比，总和为 100）
QUESTION_WEIGHTS: dict[str, int] = {
    "factual": 40,  # 40% 简单事实型
    "reasoning": 30,  # 30% 复杂推理型
    "tool_call": 20,  # 20% 工具调用型
    "no_answer": 10,  # 10% 无答案型
}


def pick_question() -> tuple[str, str]:
    """按权重随机选择问题类型与具体问题。

    使用 random.choices 按权重抽样，确保问题类型分布符合
    40/30/20/10 的目标比例。

    Returns:
        (question_type, question_text) 二元组。
        question_type 为 factual/reasoning/tool_call/no_answer 之一。
    """
    types = list(QUESTION_WEIGHTS.keys())
    weights = list(QUESTION_WEIGHTS.values())
    q_type = random.choices(types, weights=weights, k=1)[0]
    question = random.choice(QUESTION_BANK[q_type])
    return q_type, question


# ============================================================
# 模型定价表 — 用于 token 成本估算
# ============================================================

# 每千 token 单价（货币单位标注于 currency 字段）
# 数据来源：各厂商官方定价页，自部署模型按 GPU 折旧估算
# 注意：价格为近似值，实际以厂商最新定价为准
MODEL_PRICING: dict[str, dict[str, Any]] = {
    "claude-sonnet-4.6": {
        "input_per_1k": 0.003,  # $3 / 1M input tokens
        "output_per_1k": 0.015,  # $15 / 1M output tokens
        "currency": "USD",
    },
    "claude-haiku-4": {
        "input_per_1k": 0.00025,  # $0.25 / 1M input tokens
        "output_per_1k": 0.00125,  # $1.25 / 1M output tokens
        "currency": "USD",
    },
    "qwen-turbo": {
        "input_per_1k": 0.0003,  # approx ¥0.3 / 1M
        "output_per_1k": 0.0006,
        "currency": "CNY",
    },
    "qwen-plus": {
        "input_per_1k": 0.0008,
        "output_per_1k": 0.002,
        "currency": "CNY",
    },
    "qwen-max": {
        "input_per_1k": 0.002,
        "output_per_1k": 0.006,
        "currency": "CNY",
    },
    "llama-3.3-70b": {
        "input_per_1k": 0.0001,  # 自部署估算
        "output_per_1k": 0.0002,
        "currency": "USD",
    },
    "qwen3-72b": {
        "input_per_1k": 0.0001,  # 自部署估算
        "output_per_1k": 0.0002,
        "currency": "USD",
    },
}

# 默认定价（无法识别模型时的兜底）
DEFAULT_PRICING: dict[str, Any] = {
    "input_per_1k": 0.001,
    "output_per_1k": 0.003,
    "currency": "USD",
}

# 输入/输出 token 占比估算
# RAG 场景下输入（system prompt + 检索上下文 + 用户问题）占比较高
INPUT_TOKEN_RATIO = 0.65
OUTPUT_TOKEN_RATIO = 0.35


def estimate_token_cost(token_count: int, model_used: str | None) -> float:
    """估算单次请求的 token 成本。

    SSE done 事件返回的 token_count 为总 token 数（输入+输出），
    此处按 RAG 场景经验比例拆分输入/输出后分别计价。

    Args:
        token_count: 总 token 数（来自 SSE done 事件）。
        model_used: 使用的模型 ID（如 "claude-sonnet-4.6"）。

    Returns:
        估算成本（浮点数，货币单位取决于模型定价表）。
    """
    pricing = MODEL_PRICING.get(model_used or "", DEFAULT_PRICING)
    input_tokens = int(token_count * INPUT_TOKEN_RATIO)
    output_tokens = token_count - input_tokens
    cost = (
        input_tokens / 1000.0 * pricing["input_per_1k"]
        + output_tokens / 1000.0 * pricing["output_per_1k"]
    )
    return round(cost, 6)


# ============================================================
# 自定义指标收集器 — 线程安全
# ============================================================


class MetricsCollector:
    """全局指标收集器 — 在 Locust 事件钩子中聚合自定义指标。

    线程安全：所有写操作通过 threading.Lock 保护。
    Locust 在 gevent 协程中运行 User 任务，但事件钩子可能跨线程，
    因此使用线程锁确保并发安全。

    收集的指标：
        - 按问题类型分组的响应延迟列表（用于计算 P50/P95/P99）
        - 按问题类型分组的错误计数与总请求数
        - TTFT（Time To First Token）统计
        - Token 消耗统计（总 token 数、总成本、按模型分组）
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # 按问题类型分组的响应时间列表（毫秒）
        self._latencies: dict[str, list[float]] = defaultdict(list)
        # 按问题类型分组的错误数
        self._errors: dict[str, int] = defaultdict(int)
        # 按问题类型分组的总请求数
        self._counts: dict[str, int] = defaultdict(int)
        # TTFT 列表（毫秒）— 全局，不按类型分组
        self._ttfts: list[float] = []
        # Token 统计
        self._total_tokens: int = 0
        self._total_cost: float = 0.0
        self._tokens_by_model: dict[str, int] = defaultdict(int)
        self._cost_by_model: dict[str, float] = defaultdict(float)

    def record(
        self,
        question_type: str,
        latency_ms: float,
        success: bool,
        ttft_ms: float | None = None,
        token_count: int = 0,
        model_used: str | None = None,
    ) -> None:
        """记录单次请求的指标。

        Args:
            question_type: 问题类型（factual/reasoning/tool_call/no_answer）。
            latency_ms: 完整响应延迟（从请求发出到 SSE done 事件，毫秒）。
            success: 请求是否成功（收到 done 事件且无 error）。
            ttft_ms: 首 token 到达时间（毫秒），None 表示未收到任何 token。
            token_count: 总 token 数（来自 done 事件）。
            model_used: 使用的模型 ID。
        """
        with self._lock:
            self._counts[question_type] += 1
            self._latencies[question_type].append(latency_ms)
            if not success:
                self._errors[question_type] += 1
            if ttft_ms is not None:
                self._ttfts.append(ttft_ms)
            if token_count > 0:
                cost = estimate_token_cost(token_count, model_used)
                self._total_tokens += token_count
                self._total_cost += cost
                model_key = model_used or "unknown"
                self._tokens_by_model[model_key] += token_count
                self._cost_by_model[model_key] += cost

    @staticmethod
    def _percentile(data: list[float], pct: float) -> float:
        """计算分位数。

        Args:
            data: 已收集的数据列表。
            pct: 百分位（0-100），如 50 表示 P50。

        Returns:
            分位数值（保留两位小数），空列表返回 0.0。
        """
        if not data:
            return 0.0
        sorted_data = sorted(data)
        idx = int(len(sorted_data) * pct / 100.0)
        if idx >= len(sorted_data):
            idx = len(sorted_data) - 1
        return round(sorted_data[idx], 2)

    def summary(self) -> dict[str, Any]:
        """生成汇总报告字典。

        Returns:
            包含所有指标的结构化字典，可直接用于控制台打印或 JSON 序列化。
        """
        with self._lock:
            all_latencies: list[float] = []
            type_stats: dict[str, Any] = {}
            for q_type, latencies in self._latencies.items():
                all_latencies.extend(latencies)
                total = self._counts[q_type]
                errors = self._errors[q_type]
                type_stats[q_type] = {
                    "count": total,
                    "errors": errors,
                    "error_rate": (
                        f"{errors / total * 100:.2f}%" if total else "0.00%"
                    ),
                    "p50_ms": self._percentile(latencies, 50),
                    "p95_ms": self._percentile(latencies, 95),
                    "p99_ms": self._percentile(latencies, 99),
                    "avg_ms": (
                        round(sum(latencies) / len(latencies), 2)
                        if latencies
                        else 0.0
                    ),
                }

            total_requests = sum(self._counts.values())
            total_errors = sum(self._errors.values())

            return {
                "total_requests": total_requests,
                "total_errors": total_errors,
                "overall_error_rate": (
                    f"{total_errors / total_requests * 100:.2f}%"
                    if total_requests
                    else "0.00%"
                ),
                "latency_ms": {
                    "p50": self._percentile(all_latencies, 50),
                    "p95": self._percentile(all_latencies, 95),
                    "p99": self._percentile(all_latencies, 99),
                    "avg": (
                        round(sum(all_latencies) / len(all_latencies), 2)
                        if all_latencies
                        else 0.0
                    ),
                },
                "ttft_ms": {
                    "p50": self._percentile(self._ttfts, 50),
                    "p95": self._percentile(self._ttfts, 95),
                    "p99": self._percentile(self._ttfts, 99),
                    "avg": (
                        round(sum(self._ttfts) / len(self._ttfts), 2)
                        if self._ttfts
                        else 0.0
                    ),
                    "count": len(self._ttfts),
                },
                "by_question_type": type_stats,
                "token_stats": {
                    "total_tokens": self._total_tokens,
                    "total_cost": round(self._total_cost, 4),
                    "avg_tokens_per_request": (
                        round(self._total_tokens / total_requests, 1)
                        if total_requests
                        else 0.0
                    ),
                    "by_model": {
                        model: {
                            "tokens": tokens,
                            "cost": round(self._cost_by_model[model], 4),
                        }
                        for model, tokens in self._tokens_by_model.items()
                    },
                },
            }


# 全局指标收集器实例（模块级单例）
metrics_collector = MetricsCollector()


# ============================================================
# SSE 流式响应解析
# ============================================================


@dataclass
class SSEResult:
    """SSE 流式响应解析结果。

    封装从 SSE 流中提取的关键指标，供 Locust 任务记录使用。
    """

    success: bool = True
    error_message: str = ""
    token_count: int = 0  # done 事件中的总 token 数
    model_used: str | None = None  # done 事件中的模型 ID
    ttft_ms: float | None = None  # 首 token 到达时间（毫秒）
    total_tokens_received: int = 0  # 接收到的 token 片段数（用于估算流式长度）
    has_done: bool = False  # 是否收到 done 事件
    has_error: bool = False  # 是否收到 error 事件
    sources_count: int = 0  # 引用来源数量
    tool_calls: int = 0  # 工具调用次数


def _process_sse_event(
    event: str | None,
    data: str,
    result: SSEResult,
) -> None:
    """处理单个 SSE 事件，更新解析结果。

    SSE 事件类型（与后端 app/utils/sse.py SSEEventType 对齐）：
        - 默认（无 event 字段）: 逐 token 文本片段
        - meta: 对话元数据
        - thinking: Agent 思考进度
        - retrieve_start/retrieve_end: 检索进度
        - tool_call_start/tool_call_end: 工具调用进度
        - sources: 引用来源
        - quality: 质量评分
        - done: 流结束标记（含 token_count / model_used）
        - error: 错误事件

    Args:
        event: 事件类型字符串，None 表示默认 data 事件。
        data: 事件数据字符串（可能是 JSON 或纯文本 token）。
        result: 待更新的 SSEResult 对象。
    """
    # 默认事件（无 event 字段）-> 逐 token 文本片段
    if event is None:
        result.total_tokens_received += 1
        return

    # 尝试解析 JSON 数据（非 token 事件通常为 JSON）
    payload: dict[str, Any] = {}
    if data:
        try:
            payload = json.loads(data)
        except (json.JSONDecodeError, ValueError):
            payload = {}

    if event == "done":
        result.has_done = True
        result.token_count = int(payload.get("token_count", 0))
        result.model_used = payload.get("model_used")

    elif event == "error":
        result.has_error = True
        result.error_message = payload.get("message", "未知错误")

    elif event == "sources":
        sources = payload.get("sources", [])
        if isinstance(sources, list):
            result.sources_count = len(sources)

    elif event == "tool_call_start":
        result.tool_calls += 1

    # 其余事件类型（meta/thinking/retrieve_start/retrieve_end/
    # tool_call_end/quality 等）暂不提取指标，仅正常消费


def parse_sse_stream(response: Any, start_time: float) -> SSEResult:
    """解析 SSE 流式响应，提取关键指标。

    逐行读取 SSE 流（response.iter_lines），按 SSE 协议解析 event/data 字段，
    在空行处分隔事件，提取 TTFT / token_count / model_used / sources / tool_calls
    等指标。

    SSE 协议要点：
        - "event: xxx" 行设置当前事件类型
        - "data: xxx" 行追加事件数据
        - 空行表示当前事件结束，触发事件处理
        - ":" 开头的行为注释（心跳保活）

    Args:
        response: requests.Response 对象（stream=True，支持 iter_lines）。
        start_time: 请求开始时间戳（time.perf_counter() 返回值）。

    Returns:
        SSEResult 解析结果对象。
    """
    result = SSEResult()
    current_event: str | None = None
    data_lines: list[str] = []

    try:
        for raw_line in response.iter_lines(decode_unicode=True):
            if raw_line is None:
                continue

            # 空行 -> 当前事件结束，处理累积的 data
            if raw_line == "":
                if data_lines:
                    data_str = "\n".join(data_lines)
                    _process_sse_event(current_event, data_str, result)
                current_event = None
                data_lines = []
                continue

            # SSE 注释行（心跳 ": heartbeat" 等），跳过
            if raw_line.startswith(":"):
                continue

            # 解析 event 字段
            if raw_line.startswith("event:"):
                current_event = raw_line[len("event:") :].strip()

            # 解析 data 字段
            elif raw_line.startswith("data:"):
                data_str = raw_line[len("data:") :].strip()
                data_lines.append(data_str)
                # 记录首 token 到达时间
                # 默认事件（current_event 为 None）的 data 即为 token 文本
                if current_event is None and result.ttft_ms is None:
                    result.ttft_ms = round(
                        (time.perf_counter() - start_time) * 1000, 2
                    )

            # id 字段暂不处理

    except Exception as exc:
        result.success = False
        result.error_message = f"SSE 流读取异常: {exc}"
        return result

    # 处理流末尾可能未以空行结束的最后一个事件
    if data_lines:
        data_str = "\n".join(data_lines)
        _process_sse_event(current_event, data_str, result)

    # 未收到 done 事件 -> 视为异常（流被截断或超时）
    if not result.has_done and not result.has_error:
        result.success = False
        result.error_message = "SSE 流未正常结束（缺少 done 事件）"

    if result.has_error:
        result.success = False
        if not result.error_message:
            result.error_message = "SSE 流返回 error 事件"

    return result


# ============================================================
# Locust 用户类 — 模拟 RAG 问答用户
# ============================================================


class RAGChatUser(HttpUser):
    """模拟企业知识库 RAG 问答用户。

    每个虚拟用户实例模拟一个真实用户，通过 SSE 流式接口发送问题
    并接收 AI 回答。问题类型按权重混合：
        - 事实型 40%    : 直接检索型问题
        - 推理型 30%    : 跨文档推理型问题
        - 工具调用型 20%: 需触发 Agent 工具调用的问题
        - 无答案型 10%  : 知识库无对应内容，测试拒答兜底

    等待时间：
        使用 between(3, 8) 模拟真实用户的阅读-思考间隔，
        避免无间隔轰炸导致服务端过载（非真实场景）。
    """

    # 用户操作间等待时间（秒）— 模拟真实用户阅读回答后的思考间隔
    wait_time = between(3, 8)

    def on_start(self) -> None:
        """用户启动时初始化认证信息与知识库配置。

        从 locust environment.parsed_options 读取自定义命令行参数，
        构建 Authorization 请求头与知识库 ID 列表。
        """
        # 读取自定义命令行参数
        raw_api_key = self.environment.parsed_options.api_key or ""
        raw_kb_ids = self.environment.parsed_options.kb_ids or ""

        # 构建 Authorization 头
        # api_key 可为 "Bearer xxx" 或裸 token，统一补全 Bearer 前缀
        if raw_api_key and not raw_api_key.startswith("Bearer "):
            self.auth_header = f"Bearer {raw_api_key}"
        else:
            self.auth_header = raw_api_key

        # 解析 kb_ids（逗号分隔的 UUID 列表）
        self.kb_id_list: list[str] = (
            [kid.strip() for kid in raw_kb_ids.split(",") if kid.strip()]
            if raw_kb_ids
            else []
        )

    def _build_headers(self) -> dict[str, str]:
        """构建 HTTP 请求头。

        Returns:
            包含 Content-Type / Accept / Authorization 的请求头字典。
        """
        headers: dict[str, str] = {
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        }
        if self.auth_header:
            headers["Authorization"] = self.auth_header
        return headers

    def _build_payload(self, question: str) -> dict[str, Any]:
        """构建 SSE 问答请求体。

        对应后端 ChatRequest schema（app/schemas/conversation.py）：
            - query: 用户提问内容（必填）
            - conversation_id: 对话 ID（None 表示新建对话）
            - agent_type: Agent 类型（qa/workflow/action）
            - scope: 检索范围限定（可选，层级过滤）

        Args:
            question: 用户问题文本。

        Returns:
            符合 ChatRequest schema 的请求体字典。
        """
        payload: dict[str, Any] = {
            "query": question,
            "conversation_id": None,  # 每次新建对话，避免历史上下文干扰压测
            "agent_type": "qa",
        }
        # 如果指定了知识库 ID，通过 scope 传递
        # 注意：chat/stream 接口的知识库权限过滤由用户 JWT 中的
        # tenant_id + 用户可访问 kb_ids 自动处理，scope 用于层级过滤
        if self.kb_id_list:
            payload["scope"] = {
                "kb_ids": self.kb_id_list,
            }
        return payload

    @task
    def chat_stream(self) -> None:
        """发送 SSE 流式问答请求 — 核心测试任务。

        完整流程：
        1. 按权重随机选择问题类型与具体问题
        2. 构建 Authorization 认证头与 ChatRequest 请求体
        3. 发起 POST /api/v1/chat/stream 请求（stream=True, catch_response=True）
        4. 逐行解析 SSE 流，提取 TTFT / token_count / model_used 等指标
        5. 记录到 Locust 内置统计（catch_response 标记成功/失败）
        6. 同时记录到自定义 MetricsCollector（延迟分位数 / token 成本等）
        7. 根据流结束状态标记请求成功/失败

        异常处理：
        - HTTP 非 200 状态码 -> 标记失败，记录状态码
        - SSE error 事件 -> 标记失败，记录错误消息
        - SSE 流未正常结束（无 done 事件）-> 标记失败
        - 网络异常 / 超时 -> catch_response 自动标记失败
        """
        question_type, question = pick_question()
        headers = self._build_headers()
        payload = self._build_payload(question)

        # 请求名称按问题类型分组，便于 Locust Web UI 分组查看
        request_name = f"chat/stream [{question_type}]"
        start_time = time.perf_counter()

        try:
            with self.client.post(
                CHAT_STREAM_PATH,
                json=payload,
                headers=headers,
                stream=True,
                catch_response=True,
                name=request_name,
                timeout=SSE_MAX_DURATION,
            ) as response:
                # HTTP 层面错误（非 200）
                if response.status_code != 200:
                    # 尝试读取错误响应体（可能为 JSON 格式的错误信息）
                    try:
                        error_body = response.text[:300] if response.text else ""
                    except Exception:
                        error_body = ""
                    error_msg = f"HTTP {response.status_code}: {error_body}"
                    response.failure(error_msg)
                    latency_ms = round(
                        (time.perf_counter() - start_time) * 1000, 2
                    )
                    metrics_collector.record(
                        question_type=question_type,
                        latency_ms=latency_ms,
                        success=False,
                    )
                    return

                # 解析 SSE 流式响应
                sse_result = parse_sse_stream(response, start_time)
                latency_ms = round(
                    (time.perf_counter() - start_time) * 1000, 2
                )

                # 标记 Locust 请求成功/失败
                if sse_result.success:
                    response.success()
                else:
                    response.failure(sse_result.error_message)

                # 记录自定义指标
                metrics_collector.record(
                    question_type=question_type,
                    latency_ms=latency_ms,
                    success=sse_result.success,
                    ttft_ms=sse_result.ttft_ms,
                    token_count=sse_result.token_count,
                    model_used=sse_result.model_used,
                )

        except Exception:
            # 请求级异常（连接超时、网络错误等）
            # catch_response 上下文管理器会自动将请求标记为失败
            # 此处仅补充记录自定义指标，避免重复触发 events.request
            latency_ms = round((time.perf_counter() - start_time) * 1000, 2)
            metrics_collector.record(
                question_type=question_type,
                latency_ms=latency_ms,
                success=False,
            )


# ============================================================
# 阶梯式加压负载形状 — 自动 10 -> 50 -> 100 并发递增
# ============================================================


class StagedLoadShape(LoadTestShape):
    """阶梯式加压负载形状 — 自动控制并发用户数阶梯递增。

    启用方式：locust 命令行添加 --staged 参数。
    未启用时（默认），回退为使用 --num-users / --spawn-rate 命令行参数。

    阶段划分（总时长约 9 分钟）：

    +-------+---------+--------+-------------+------------------------+
    | 阶段  | 时间(s) | 用户数 | spawn-rate  | 说明                   |
    +=======+=========+========+=============+========================+
    |   1   |  0-60   |   10   |     2/s     | 基线探测，低并发预热    |
    |   2   | 60-180  |   50   |     5/s     | 中等压力，观察降级点    |
    |   3   |180-360  |  100   |    10/s     | 峰值压力，寻找瓶颈      |
    |   4   |360-480  |  100   |    10/s     | 稳态观察，持续峰值      |
    |   5   |480-540  |    0   |    10/s     | 优雅退出，降为 0        |
    +-------+---------+--------+-------------+------------------------+

    每个阶段的用户数和递增速率可根据实际环境调整 STAGED_PROFILE 常量。
    """

    def tick(self) -> tuple[int, float] | None:
        """每次 tick 返回当前阶段的目标用户数与递增速率。

        Returns:
            (users, spawn_rate) 元组，或 None 表示测试结束。
        """
        # 未启用阶梯模式 -> 回退到命令行参数
        if not getattr(self.environment.parsed_options, "staged", False):
            num_users = self.environment.parsed_options.num_users or 1
            spawn_rate = self.environment.parsed_options.spawn_rate or 1
            return (num_users, float(spawn_rate))

        run_time = self.get_run_time()

        for stage in STAGED_PROFILE:
            if run_time < stage["end_time"]:
                return (stage["users"], float(stage["spawn_rate"]))

        # 所有阶段结束 -> 停止测试
        return None


# ============================================================
# Locust 事件钩子 — CLI 参数注册 & 报告输出
# ============================================================


@events.init_command_line_parser.add_listener
def _register_custom_args(parser: Any) -> None:
    """注册自定义命令行参数。

    locust 内置 --host / --num-users / --spawn-rate / --run-time 等参数，
    此处仅注册本项目专用的 --api-key / --kb-ids / --staged。

    Args:
        parser: locust 的命令行参数解析器实例。
    """
    parser.add_argument(
        "--api-key",
        type=str,
        default="",
        help=(
            "API 认证令牌（JWT Bearer Token）。"
            "格式：'Bearer eyJhbGciOi...' 或直接传入 token 值（自动补 Bearer 前缀）。"
            "对应请求头 Authorization: Bearer <token>。"
        ),
    )
    parser.add_argument(
        "--kb-ids",
        type=str,
        default="",
        help=(
            "知识库 ID 列表（逗号分隔），如 'uuid1,uuid2'。"
            "用于限定检索范围；为空则使用用户默认可访问的全部知识库。"
        ),
    )
    parser.add_argument(
        "--staged",
        action="store_true",
        default=False,
        help=(
            "启用阶梯式加压模式（10 -> 50 -> 100 并发用户自动递增）。"
            "启用后忽略 --num-users / --spawn-rate，按 STAGED_PROFILE 定义执行。"
        ),
    )


@events.test_start.add_listener
def _on_test_start(environment: Any, **kwargs: Any) -> None:
    """测试开始时打印配置信息摘要。

    Args:
        environment: locust Environment 实例。
        **kwargs: 其他事件参数（忽略）。
    """
    print("\n" + "=" * 70)
    print("  RAG 系统 SSE 流式问答接口 — 负载测试启动")
    print("=" * 70)
    print(f"  目标地址      : {environment.host}")
    print(f"  接口路径      : {CHAT_STREAM_PATH}")

    api_key = environment.parsed_options.api_key or "(未设置)"
    kb_ids = environment.parsed_options.kb_ids or "(未设置)"
    staged = getattr(environment.parsed_options, "staged", False)

    # 脱敏打印 api-key（仅显示前 20 字符）
    if api_key and len(api_key) > 20:
        api_key_display = api_key[:20] + "..."
    else:
        api_key_display = api_key

    print(f"  API Key       : {api_key_display}")
    print(f"  知识库 ID     : {kb_ids}")
    print(f"  阶梯加压      : {'是 (10->50->100)' if staged else '否'}")

    if not staged:
        print(
            f"  并发用户数    : {environment.parsed_options.num_users}"
        )
        print(
            f"  递增速率      : {environment.parsed_options.spawn_rate}/s"
        )

    print(
        "  问题类型分布  : 事实型 40% / 推理型 30% / "
        "工具调用型 20% / 无答案型 10%"
    )
    print("=" * 70 + "\n")


@events.test_stop.add_listener
def _on_test_stop(environment: Any, **kwargs: Any) -> None:
    """测试结束时输出自定义指标汇总报告。

    打印内容包括：
    - 总体统计（总请求数 / 错误数 / 错误率）
    - 响应延迟分位数（P50 / P95 / P99 / Avg）
    - 首 token 到达时间分位数（TTFT P50 / P95 / P99）
    - 按问题类型分组的详细统计
    - Token 消耗与成本估算（按模型分组）

    Args:
        environment: locust Environment 实例。
        **kwargs: 其他事件参数（忽略）。
    """
    summary = metrics_collector.summary()

    print("\n" + "=" * 70)
    print("  负载测试完成 — 自定义指标汇总")
    print("=" * 70)

    # --- 总体统计 ---
    print("\n  [总体统计]")
    print(f"    总请求数      : {summary['total_requests']}")
    print(f"    总错误数      : {summary['total_errors']}")
    print(f"    错误率        : {summary['overall_error_rate']}")

    # --- 响应延迟分位数 ---
    lat = summary["latency_ms"]
    print("\n  [响应延迟分位数 — 完整 SSE 流时长（请求发出 -> done 事件）]")
    print(f"    P50  : {lat['p50']:.2f} ms")
    print(f"    P95  : {lat['p95']:.2f} ms")
    print(f"    P99  : {lat['p99']:.2f} ms")
    print(f"    Avg  : {lat['avg']:.2f} ms")

    # --- TTFT 分位数 ---
    ttft = summary["ttft_ms"]
    print(f"\n  [首 Token 到达时间 (TTFT) — 共 {ttft['count']} 个样本]")
    print(f"    P50  : {ttft['p50']:.2f} ms")
    print(f"    P95  : {ttft['p95']:.2f} ms")
    print(f"    P99  : {ttft['p99']:.2f} ms")
    print(f"    Avg  : {ttft['avg']:.2f} ms")

    # --- 按问题类型分组 ---
    print("\n  [按问题类型分组]")
    header = (
        f"    {'类型':<12} {'数量':>6} {'错误':>6} {'错误率':>8} "
        f"{'P50(ms)':>10} {'P95(ms)':>10} {'P99(ms)':>10} {'Avg(ms)':>10}"
    )
    print(header)
    separator = (
        f"    {'-' * 12} {'-' * 6} {'-' * 6} {'-' * 8} "
        f"{'-' * 10} {'-' * 10} {'-' * 10} {'-' * 10}"
    )
    print(separator)
    for q_type, stats in summary["by_question_type"].items():
        print(
            f"    {q_type:<12} {stats['count']:>6} {stats['errors']:>6} "
            f"{stats['error_rate']:>8} {stats['p50_ms']:>10.2f} "
            f"{stats['p95_ms']:>10.2f} {stats['p99_ms']:>10.2f} "
            f"{stats['avg_ms']:>10.2f}"
        )

    # --- Token 消耗与成本估算 ---
    token_stats = summary["token_stats"]
    print("\n  [Token 消耗与成本估算]")
    print(f"    总 Token 数        : {token_stats['total_tokens']:,}")
    print(f"    总成本估算         : {token_stats['total_cost']:.4f}")
    print(
        f"    平均每请求 Token   : {token_stats['avg_tokens_per_request']:.1f}"
    )
    if token_stats["by_model"]:
        print("\n    [按模型分组]")
        model_header = (
            f"      {'模型':<22} {'Token 数':>12} {'成本':>12}"
        )
        print(model_header)
        print(
            f"      {'-' * 22} {'-' * 12} {'-' * 12}"
        )
        for model, info in token_stats["by_model"].items():
            print(
                f"      {model:<22} {info['tokens']:>12,} "
                f"{info['cost']:>12.4f}"
            )

    print("\n" + "=" * 70)
    print(
        "  提示：Locust 内置统计（QPS / 延迟分位数 / 失败率）"
        "请查看 Web UI 或 --csv 导出"
    )
    print("=" * 70 + "\n")
