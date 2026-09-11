"""任务欠投递发件箱（轻量 Outbox）— 持久化派发失败的一次性 Celery 触发。

设计要点：
    - 仅覆盖"派发失败仅日志"的一次性触发链路（好评→FAQ 回流、采纳→FAQ 回流、
      解析→智能处理链等），不追求分布式事务强一致（正常路径不写 outbox）；
    - 派发失败时以独立短事务写入本表，由 flush_task_outbox 定时补投；
    - 任务按名字派发（celery_app.send_task），避免与业务任务模块循环导入；
    - 补投上限 MAX_ATTEMPTS 次，超限标记 dead，防止毒丸消息无限重试。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDMixin


class TaskOutbox(UUIDMixin, TimestampMixin, Base):
    """任务欠投递发件箱表。

    字段说明：
    - task_name：Celery 任务全名（如 tasks.compounding_tasks.xxx）；
    - task_kwargs：派发参数（JSONB）；
    - status：pending（待补投）/ sent（已派发）/ dead（超过重试上限）；
    - attempts：已补投次数；
    - last_error：最近一次派发失败原因；
    - sent_at：补投成功时间。
    """

    __tablename__ = "task_outbox"

    task_name: Mapped[str] = mapped_column(
        String(200), index=True, nullable=False, comment="Celery 任务全名"
    )
    task_kwargs: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, comment="派发参数"
    )
    status: Mapped[str] = mapped_column(
        String(20), index=True, nullable=False, default="pending", comment="pending/sent/dead"
    )
    attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, comment="已补投次数"
    )
    last_error: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="最近一次派发失败原因"
    )
    sent_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="补投成功时间"
    )
