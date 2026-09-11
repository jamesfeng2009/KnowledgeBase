"""提交幂等公共工具 — 单一职责：唯一约束识别与输入指纹计算。

设计要点（对应"超时重试防重复建任务"验收）：
    - 防重复的最后一道闸门是数据库部分唯一索引，前置查询只是快捷路径；
    - IntegrityError 必须精确识别到"那一条"幂等唯一约束，其余约束
      （主键冲突/外键错误等）原样上抛，绝不能被翻译成"重复提交"；
    - input_hash 从校验后的输入按规范化 JSON 计算（键排序 + 紧凑分隔符），
      同一份输入换字段顺序得到同一指纹。
"""

from __future__ import annotations

import hashlib
import json

from sqlalchemy.exc import IntegrityError


class IdempotencyConflictError(Exception):
    """同一幂等键对应不同的提交内容 — 映射为 409 IDEMPOTENCY_CONFLICT。"""


def canonical_input_hash(payload: dict) -> str:
    """计算提交内容指纹 — 规范化 JSON（键排序、紧凑分隔符）的 SHA-256。"""
    canonical = json.dumps(
        payload,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def is_unique_violation(exc: IntegrityError, constraint_name: str) -> bool:
    """判断异常是否为指定唯一约束冲突 — 只认具体约束，不做宽泛翻译。

    asyncpg 异常带 constraint_name 属性；拿不到属性时退化为对异常文本的
    子串匹配（SQLAlchemy 异步路径下两种驱动表现统一）。
    """
    orig = getattr(exc, "orig", None)
    if orig is not None:
        name = getattr(orig, "constraint_name", None)
        if name is not None:
            return name == constraint_name
        # diag.diag 内层（某些驱动包装）
        diag = getattr(orig, "diag", None)
        name = getattr(diag, "constraint_name", None) if diag else None
        if name is not None:
            return name == constraint_name
    return constraint_name in str(exc)
