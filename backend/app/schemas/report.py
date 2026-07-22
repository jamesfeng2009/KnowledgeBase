"""
报表 Schema — 单一职责：使用量、知识库与成本报表的数据验证与序列化。

遵循单一职责：仅负责入参校验与出参序列化，
不包含数据聚合、SQL 查询等业务逻辑。
"""

from __future__ import annotations

from datetime import date
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field


class MetricType(str, Enum):
    """报表指标类型。"""

    queries = "queries"
    users = "users"
    tokens = "tokens"
    cost = "cost"


class GroupBy(str, Enum):
    """分组维度。"""

    day = "day"
    week = "week"
    month = "month"


class ReportFilter(BaseModel):
    """报表查询过滤参数。"""

    model_config = ConfigDict(from_attributes=True)

    start_date: date = Field(..., description="起始日期（包含）")
    end_date: date = Field(..., description="结束日期（包含）")
    group_by: GroupBy = Field(default=GroupBy.day, description="分组维度")
    metric_type: MetricType = Field(
        default=MetricType.queries, description="指标类型"
    )


class UsageReport(BaseModel):
    """使用量报表。"""

    model_config = ConfigDict(from_attributes=True)

    period: str = Field(..., description="统计周期标识")
    total_queries: int = Field(default=0, ge=0, description="总查询次数")
    unique_users: int = Field(default=0, ge=0, description="独立用户数")
    avg_response_time: float = Field(
        default=0.0, ge=0.0, description="平均响应时间（秒）"
    )
    total_tokens: int = Field(default=0, ge=0, description="总 token 消耗")
    cost: float = Field(default=0.0, ge=0.0, description="成本（元）")


class UsageReportSeries(BaseModel):
    """使用量报表时间序列。"""

    model_config = ConfigDict(from_attributes=True)

    filter: ReportFilter = Field(..., description="查询过滤条件")
    items: list[UsageReport] = Field(
        default_factory=list, description="按时间分组的报表数据"
    )
    summary: UsageReport | None = Field(
        default=None, description="汇总行（整个时间范围合计）"
    )


class KnowledgeReport(BaseModel):
    """知识库报表。"""

    model_config = ConfigDict(from_attributes=True)

    total_docs: int = Field(default=0, ge=0, description="文档总数")
    total_kbs: int = Field(default=0, ge=0, description="知识库总数")
    avg_quality_score: float = Field(
        default=0.0, ge=0.0, le=100.0, description="平均质量分（0-100）"
    )
    gap_count: int = Field(default=0, ge=0, description="知识缺口数量")
    expiring_count: int = Field(
        default=0, ge=0, description="即将过期文档数量"
    )


class CostReport(BaseModel):
    """成本报表。"""

    model_config = ConfigDict(from_attributes=True)

    period: str = Field(..., description="统计周期标识")
    total_cost: float = Field(default=0.0, ge=0.0, description="总成本（元）")
    total_input_tokens: int = Field(default=0, ge=0, description="总输入 token")
    total_output_tokens: int = Field(default=0, ge=0, description="总输出 token")
    by_model: dict[str, float] = Field(
        default_factory=dict, description="按模型分组的成本（元）"
    )
    by_request_type: dict[str, float] = Field(
        default_factory=dict, description="按请求类型分组的成本（元）"
    )


class CostReportSeries(BaseModel):
    """成本报表时间序列。"""

    model_config = ConfigDict(from_attributes=True)

    filter: ReportFilter = Field(..., description="查询过滤条件")
    items: list[CostReport] = Field(
        default_factory=list, description="按时间分组的成本数据"
    )
    summary: CostReport | None = Field(
        default=None, description="汇总行（整个时间范围合计）"
    )
