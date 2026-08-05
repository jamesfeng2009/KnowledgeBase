"""
高风险信息检测器 — 对金额、日期、法律条款等高风险信息进行二次核验。

核心思路：
    LLM 生成的答案中如果包含金额、日期、法律条款等高风险信息，
    必须与检索到的知识库文档进行一致性校验，防止幻觉导致错误信息传播。

检测维度：
    1. 金额（amount）：数字 + 货币单位（元/美元/欧元/USD/EUR 等）
    2. 日期（date）：ISO 日期、中文日期、斜杠日期等
    3. 法律条款（legal_term）：合同、协议、条款、法律、法规等关键词

核验逻辑：
    从答案中提取高风险信息 → 在来源文档中搜索匹配 → 未匹配的标记为风险

遵循单一职责：本模块只负责高风险信息检测与核验，不做答案拦截或重生成。
遵循优雅降级：LLM 不可用时仅做规则匹配，不阻断主流程。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from app.utils.logger import get_logger

log = get_logger(__name__)


@dataclass
class HighRiskItem:
    """单个高风险信息项。

    Attributes:
        type: 风险类型 "amount" / "date" / "legal_term"。
        value: 原始文本值。
        start: 在答案中的起始位置。
        end: 在答案中的结束位置。
        verified: 是否在来源文档中找到匹配。
        source_snippet: 匹配到的来源文档片段（verified=True 时有值）。
        risk_level: P1-8 三档分级（未核验项有效）：
            "low"（偏差 <1%，警告）/ "medium"（偏差 1-10%，提示核实）/
            "high"（偏差 >10% 或完全编造，阻断）。已核验项为空串。
        deviation: 金额偏差幅度（仅 amount 类型未核验时有值，0-1 小数）。
    """

    type: str
    value: str
    start: int
    end: int
    verified: bool = False
    source_snippet: str = ""
    risk_level: str = ""
    deviation: float | None = None

    def to_dict(self) -> dict[str, Any]:
        """转为字典。"""
        return {
            "type": self.type,
            "value": self.value,
            "start": self.start,
            "end": self.end,
            "verified": self.verified,
            "source_snippet": self.source_snippet,
            "risk_level": self.risk_level,
            "deviation": self.deviation,
        }


@dataclass
class HighRiskResult:
    """高风险信息核验结果。

    Attributes:
        items: 检测到的高风险信息项列表。
        unverified_count: 未通过核验的信息项数量。
        has_risk: 是否存在未核验的高风险信息。
        action: 建议动作 "pass" / "warn" / "block"。
    """

    items: list[HighRiskItem] = field(default_factory=list)
    unverified_count: int = 0
    has_risk: bool = False
    action: str = "pass"  # pass / warn / confirm / block

    @property
    def total_count(self) -> int:
        """检测到的高风险信息总数。"""
        return len(self.items)

    @property
    def max_risk_level(self) -> str:
        """未核验项中的最高风险等级（P1-8），无风险时返回空串。"""
        order = {"": 0, "low": 1, "medium": 2, "high": 3}
        unverified = [i for i in self.items if not i.verified]
        if not unverified:
            return ""
        return max((i.risk_level for i in unverified), key=lambda lv: order.get(lv, 0))

    def to_dict(self) -> dict[str, Any]:
        """转为字典。"""
        return {
            "items": [item.to_dict() for item in self.items],
            "total_count": self.total_count,
            "unverified_count": self.unverified_count,
            "has_risk": self.has_risk,
            "action": self.action,
            "max_risk_level": self.max_risk_level,
        }


class HighRiskDetector:
    """高风险信息检测器 — 金额/日期/法律条款的提取与核验。

    使用方式::

        detector = HighRiskDetector()
        items = detector.detect_high_risk_terms(answer)
        result = detector.verify_against_sources(answer, retrieved_docs)
        if result.has_risk:
            # 拦截或标记答案
            ...
    """

    #: 金额正则 — 数字 + 货币单位（中英文）
    _AMOUNT_PATTERN: re.Pattern[str] = re.compile(
        r"(\d[\d,]*(?:\.\d+)?)\s*"
        r"(元|万元|亿元|美元|欧元|人民币|港币|日元|英镑|"
        r"USD|EUR|CNY|RMB|JPY|GBP|HKD)",
        re.IGNORECASE,
    )

    #: 日期正则 — ISO 日期 / 斜杠日期 / 中文日期
    _DATE_PATTERN: re.Pattern[str] = re.compile(
        r"("
        r"\d{4}[-/年]\d{1,2}[-/月]\d{1,2}[日]?"
        r"|"
        r"\d{1,2}/\d{1,2}/\d{4}"
        r"|"
        r"\d{4}年\d{1,2}月\d{1,2}日"
        r")"
    )

    #: 法律条款关键词
    _LEGAL_TERMS: set[str] = {
        "合同", "协议", "条款", "法律", "法规", "权利", "义务",
        "违约", "赔偿", "解除", "终止", "生效", "失效",
        "甲方", "乙方", "知识产权", "保密", "竞业",
    }

    #: P1-8 三档分级阈值（金额偏差幅度，相对来源值的比例）
    #: <1% → low（警告）；1%-10% → medium（提示核实）；>10% → high（阻断）
    _DEVIATION_LOW: float = 0.01
    _DEVIATION_MEDIUM: float = 0.10

    #: P1-8 未核验 date / legal_term 的默认风险等级
    #: 日期与法律关键词无法计算数值偏差，且多为上下文性提及，默认 low（警告）
    _DEFAULT_UNVERIFIED_LEVEL: dict[str, str] = {
        "date": "low",
        "legal_term": "low",
    }

    def detect_high_risk_terms(self, text: str) -> list[HighRiskItem]:
        """检测文本中的高风险信息。

        Args:
            text: 待检测的文本（通常是 LLM 生成的答案）。

        Returns:
            检测到的高风险信息项列表。
        """
        items: list[HighRiskItem] = []

        # 检测金额
        for match in self._AMOUNT_PATTERN.finditer(text):
            items.append(HighRiskItem(
                type="amount",
                value=match.group(0),
                start=match.start(),
                end=match.end(),
            ))

        # 检测日期
        for match in self._DATE_PATTERN.finditer(text):
            items.append(HighRiskItem(
                type="date",
                value=match.group(0),
                start=match.start(),
                end=match.end(),
            ))

        # 检测法律条款关键词
        for term in self._LEGAL_TERMS:
            start = 0
            while True:
                idx = text.find(term, start)
                if idx == -1:
                    break
                items.append(HighRiskItem(
                    type="legal_term",
                    value=term,
                    start=idx,
                    end=idx + len(term),
                ))
                start = idx + len(term)

        log.debug(
            "high_risk.detected",
            total=len(items),
            amounts=sum(1 for i in items if i.type == "amount"),
            dates=sum(1 for i in items if i.type == "date"),
            legal_terms=sum(1 for i in items if i.type == "legal_term"),
        )
        return items

    def verify_against_sources(
        self,
        answer: str,
        sources: list[dict[str, Any]],
    ) -> HighRiskResult:
        """核验答案中的高风险信息与来源文档的一致性。

        核验逻辑：
            1. 从答案中提取所有高风险信息项；
            2. 对每个信息项，在来源文档内容中搜索精确匹配；
            3. 未找到匹配的标记为 unverified；
            4. 根据 unverified 比例决定建议动作。

        Args:
            answer: LLM 生成的答案文本。
            sources: 检索到的文档来源列表。

        Returns:
            HighRiskResult: 核验结果。
        """
        items = self.detect_high_risk_terms(answer)

        if not items:
            return HighRiskResult(
                items=[],
                unverified_count=0,
                has_risk=False,
                action="pass",
            )

        # 拼接所有来源文档内容用于匹配
        source_texts: list[str] = []
        for doc in sources:
            content = doc.get("content") or doc.get("text") or doc.get("snippet") or ""
            if content:
                source_texts.append(str(content))

        # 如果没有来源文档，所有高风险信息都无法核验
        if not source_texts:
            for item in items:
                item.verified = False
            return HighRiskResult(
                items=items,
                unverified_count=len(items),
                has_risk=True,
                action="warn",
            )

        # 对每个高风险信息项进行核验
        for item in items:
            item.verified = self._verify_item(item, source_texts)
            if item.verified:
                item.source_snippet = self._find_source_snippet(item, source_texts)
            else:
                # P1-8: 未核验项按偏差幅度三档分级
                item.risk_level, item.deviation = self._classify_risk_level(
                    item, source_texts
                )

        unverified = [item for item in items if not item.verified]
        unverified_count = len(unverified)
        total = len(items)
        unverified_ratio = unverified_count / total if total > 0 else 0.0

        # P1-8 三档分级决策：
        #   任一 high（金额偏差 >10% / 完全编造）→ block
        #   任一 medium（偏差 1-10%）→ confirm（提示核实）
        #   其余未核验（low）→ warn
        levels = {item.risk_level for item in unverified}
        if "high" in levels:
            action = "block"
        elif "medium" in levels:
            action = "confirm"
        elif unverified_count > 0:
            action = "warn"
        else:
            action = "pass"

        log.info(
            "high_risk.verified",
            total=total,
            verified=total - unverified_count,
            unverified=unverified_count,
            ratio=round(unverified_ratio, 2),
            action=action,
            unverified_types=list({item.type for item in unverified}),
            risk_levels={lv: sum(1 for i in unverified if i.risk_level == lv) for lv in levels},
        )

        return HighRiskResult(
            items=items,
            unverified_count=unverified_count,
            has_risk=unverified_count > 0,
            action=action,
        )

    @staticmethod
    def _verify_item(item: HighRiskItem, source_texts: list[str]) -> bool:
        """验证单个高风险信息项是否在来源文档中出现。

        对于法律条款关键词，使用模糊匹配（关键词出现在来源中即可）；
        对于金额和日期，使用精确值匹配。
        """
        for source_text in source_texts:
            if item.type == "legal_term":
                # 法律条款关键词：在来源中存在即可
                if item.value in source_text:
                    return True
            else:
                # 金额和日期：提取核心数值部分进行匹配
                # 去掉空格后比较，避免格式差异
                core_value = item.value.replace(" ", "").replace(",", "")
                source_core = source_text.replace(" ", "").replace(",", "")
                if core_value in source_core:
                    return True
                # 金额：同单位数值相等视为匹配（避免 "5" 误命中 "50000"）
                if item.type == "amount":
                    item_amount = HighRiskDetector._parse_amount(item.value)
                    if item_amount is not None:
                        for match in HighRiskDetector._AMOUNT_PATTERN.finditer(
                            source_text
                        ):
                            src_amount = HighRiskDetector._parse_amount(
                                match.group(0)
                            )
                            if (
                                src_amount is not None
                                and src_amount[1] == item_amount[1]
                                and src_amount[0] == item_amount[0]
                            ):
                                return True
        return False

    @classmethod
    def _classify_risk_level(
        cls,
        item: HighRiskItem,
        source_texts: list[str],
    ) -> tuple[str, float | None]:
        """P1-8: 对未核验项按偏差幅度三档分级。

        分级规则：
            - amount：与来源中同单位金额比较，取最小相对偏差
              <1% → low（警告）；1%-10% → medium（提示核实）；
              >10% 或来源中无同单位金额（完全编造）→ high（阻断）
            - date / legal_term：无数值偏差可算，默认 low（警告）

        Args:
            item: 未核验的高风险信息项。
            source_texts: 来源文档内容列表。

        Returns:
            (risk_level, deviation) 元组；deviation 仅 amount 有值。
        """
        if item.type != "amount":
            return cls._DEFAULT_UNVERIFIED_LEVEL.get(item.type, "low"), None

        answer_amount = cls._parse_amount(item.value)
        if answer_amount is None:
            return "high", None
        answer_value, answer_unit = answer_amount

        # 收集来源中所有同单位金额
        source_values: list[float] = []
        for source_text in source_texts:
            for match in cls._AMOUNT_PATTERN.finditer(source_text):
                parsed = cls._parse_amount(match.group(0))
                if parsed is not None and parsed[1] == answer_unit:
                    source_values.append(parsed[0])

        if not source_values:
            # 来源中无同单位金额 — 完全编造，最高风险
            return "high", None

        # 取相对偏差最小的来源值（偏差 = |答案值 - 来源值| / 来源值）
        deviation = min(
            abs(answer_value - src) / src for src in source_values if src > 0
        ) if any(src > 0 for src in source_values) else 1.0

        if deviation < cls._DEVIATION_LOW:
            return "low", deviation
        if deviation <= cls._DEVIATION_MEDIUM:
            return "medium", deviation
        return "high", deviation

    @staticmethod
    def _parse_amount(text: str) -> tuple[float, str] | None:
        """从金额文本中解析 (数值, 单位)。

        单位直接以原文匹配（元 ≠ 万元，不跨单位比较，避免误配）。

        Args:
            text: 金额文本（如 "50000元"、"100 USD"）。

        Returns:
            (数值, 单位) 或解析失败时 None。
        """
        match = re.search(
            r"(\d[\d,]*(?:\.\d+)?)\s*"
            r"(元|万元|亿元|美元|欧元|人民币|港币|日元|英镑|"
            r"USD|EUR|CNY|RMB|JPY|GBP|HKD)",
            text,
            re.IGNORECASE,
        )
        if not match:
            return None
        try:
            value = float(match.group(1).replace(",", ""))
        except ValueError:
            return None
        return value, match.group(2).lower()

    @staticmethod
    def _find_source_snippet(
        item: HighRiskItem,
        source_texts: list[str],
    ) -> str:
        """在来源文档中找到匹配项后，提取上下文片段。"""
        for source_text in source_texts:
            if item.value in source_text:
                idx = source_text.find(item.value)
                start = max(0, idx - 30)
                end = min(len(source_text), idx + len(item.value) + 30)
                return source_text[start:end]
            # 模糊匹配（去空格）
            core_value = item.value.replace(" ", "").replace(",", "")
            source_core = source_text.replace(" ", "").replace(",", "")
            if core_value in source_core:
                idx = source_core.find(core_value)
                start = max(0, idx - 30)
                end = min(len(source_core), idx + len(core_value) + 30)
                return source_core[start:end]
        return ""
