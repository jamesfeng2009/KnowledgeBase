"""
微调数据清洗工具 — 单一职责：PII 脱敏、内容哈希去重、长度过滤。

本模块全部为纯函数/轻量类，不依赖 DB 与外部服务，便于单元测试。

清洗约定（与 dataset_builder 的过滤统计口径一致）：
- PII：只脱敏改写 + 打标记（meta.pii_masked=True），不剔除样本；
- 密级 / 重复 / 长度越界：剔除样本，按原因计入 filtered_stats。
"""

from __future__ import annotations

import hashlib
import re

#: 样本默认最小字符数（question/answer 等单字段）
MIN_SAMPLE_CHARS: int = 10
#: 样本默认最大字符数（防止超长样本撑爆训练窗口）
MAX_SAMPLE_CHARS: int = 8000

# ------------------------------------------------------------------
# PII 正则 — 手机号 / 身份证 / 邮箱 / 银行卡
# ------------------------------------------------------------------
#: 中国大陆手机号：1 开头 + 第二位 3-9，共 11 位，前后边界不能是数字
_PHONE_RE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
#: 身份证：17 位数字 + 校验位（数字或 X），前后边界不能是数字/字母
_IDCARD_RE = re.compile(r"(?<![0-9A-Za-z])\d{17}[\dXx](?![0-9A-Za-z])")
#: 邮箱
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
#: 银行卡：16-19 位连续数字，前后边界不能是数字
_BANKCARD_RE = re.compile(r"(?<!\d)\d{16,19}(?!\d)")

#: 脱敏占位符
MASK_PHONE = "[PHONE]"
MASK_IDCARD = "[IDCARD]"
MASK_EMAIL = "[EMAIL]"
MASK_BANKCARD = "[BANKCARD]"


def mask_pii(text: str) -> str:
    """对文本做 PII 脱敏改写。

    替换顺序至关重要：身份证（18 位）先于银行卡（16-19 位），
    否则身份证号会被银行卡正则抢先吞并；手机号（11 位）放最后，
    其边界断言已保证不会命中长数字串内部。

    Args:
        text: 原始文本。

    Returns:
        脱敏后的文本（未命中时原样返回）。
    """
    if not text:
        return text
    masked = _IDCARD_RE.sub(MASK_IDCARD, text)
    masked = _BANKCARD_RE.sub(MASK_BANKCARD, masked)
    masked = _PHONE_RE.sub(MASK_PHONE, masked)
    masked = _EMAIL_RE.sub(MASK_EMAIL, masked)
    return masked


def content_hash(text: str) -> str:
    """计算文本内容哈希（SHA-256），作为样本去重键。

    先 strip 归一化首尾空白，避免"同内容不同换行"被判为不同样本。
    """
    return hashlib.sha256(text.strip().encode("utf-8")).hexdigest()


def check_length(
    text: str,
    min_chars: int = MIN_SAMPLE_CHARS,
    max_chars: int = MAX_SAMPLE_CHARS,
) -> str | None:
    """长度过滤检查。

    Returns:
        None 表示通过；"too_short" / "too_long" 表示剔除原因。
    """
    n = len(text.strip())
    if n < min_chars:
        return "too_short"
    if n > max_chars:
        return "too_long"
    return None


class DedupFilter:
    """哈希去重过滤器 — 同一次构建任务内按内容哈希去重。"""

    def __init__(self) -> None:
        self._seen: set[str] = set()

    def is_duplicate(self, text: str) -> bool:
        """判断文本是否已出现；首次出现时记录并返回 False。"""
        h = content_hash(text)
        if h in self._seen:
            return True
        self._seen.add(h)
        return False


def new_filtered_stats() -> dict[str, int]:
    """初始化过滤统计字典 — key 为剔除/标记原因，value 为计数。

    - classification / duplicate / too_short / too_long：剔除类；
    - pii_masked：标记类（样本保留，仅脱敏改写）。
    """
    return {
        "classification": 0,
        "duplicate": 0,
        "too_short": 0,
        "too_long": 0,
        "pii_masked": 0,
    }
