"""高风险信息检测器测试 — app/context/high_risk_detector.py。

覆盖范围：
    - detect_high_risk_terms: 金额/日期/法律条款检测
    - verify_against_sources: 高风险信息与来源文档一致性核验
    - HighRiskItem / HighRiskResult: 数据类和 to_dict
"""
from __future__ import annotations

import sys
from unittest.mock import MagicMock

import pytest

# Mock celery
if "celery" not in sys.modules:
    mock_celery = MagicMock()
    mock_celery.Celery = MagicMock
    sys.modules["celery"] = mock_celery
if "celery_app" not in sys.modules:
    mock_celery_app = MagicMock()
    mock_celery_app.celery_app = MagicMock()
    sys.modules["celery_app"] = mock_celery_app

from app.context.high_risk_detector import (
    HighRiskDetector,
    HighRiskItem,
    HighRiskResult,
)


class TestDetectHighRiskTerms:
    """detect_high_risk_terms 方法测试。"""

    def test_detect_amount(self) -> None:
        """检测金额。"""
        detector = HighRiskDetector()
        items = detector.detect_high_risk_terms("合同金额为 50000元")
        amounts = [i for i in items if i.type == "amount"]
        assert len(amounts) == 1
        assert "50000" in amounts[0].value
        assert "元" in amounts[0].value

    def test_detect_usd_amount(self) -> None:
        """检测美元金额。"""
        detector = HighRiskDetector()
        items = detector.detect_high_risk_terms("The price is $100 USD")
        amounts = [i for i in items if i.type == "amount"]
        assert len(amounts) >= 1

    def test_detect_date_iso(self) -> None:
        """检测 ISO 日期。"""
        detector = HighRiskDetector()
        items = detector.detect_high_risk_terms("合同签订于 2024-01-15")
        dates = [i for i in items if i.type == "date"]
        assert len(dates) == 1
        assert "2024-01-15" in dates[0].value

    def test_detect_date_chinese(self) -> None:
        """检测中文日期。"""
        detector = HighRiskDetector()
        items = detector.detect_high_risk_terms("合同签订于 2024年1月15日")
        dates = [i for i in items if i.type == "date"]
        assert len(dates) == 1

    def test_detect_legal_terms(self) -> None:
        """检测法律条款关键词。"""
        detector = HighRiskDetector()
        items = detector.detect_high_risk_terms("本合同包含保密条款和竞业协议")
        legal = [i for i in items if i.type == "legal_term"]
        terms = {i.value for i in legal}
        assert "合同" in terms
        assert "保密" in terms
        assert "条款" in terms
        assert "协议" in terms
        assert "竞业" in terms

    def test_detect_no_high_risk(self) -> None:
        """无高风险信息的文本返回空列表。"""
        detector = HighRiskDetector()
        items = detector.detect_high_risk_terms("这是一个普通的帮助文档。")
        assert len(items) == 0

    def test_detect_multiple_types(self) -> None:
        """同时检测多种类型的高风险信息。"""
        detector = HighRiskDetector()
        text = "合同金额 50000元，签订日期 2024-01-15，包含保密条款"
        items = detector.detect_high_risk_terms(text)
        types = {i.type for i in items}
        assert "amount" in types
        assert "date" in types
        assert "legal_term" in types

    def test_item_positions(self) -> None:
        """检测项的位置信息正确。"""
        detector = HighRiskDetector()
        text = "金额 100元"
        items = detector.detect_high_risk_terms(text)
        amounts = [i for i in items if i.type == "amount"]
        assert len(amounts) == 1
        assert amounts[0].start >= 0
        assert amounts[0].end > amounts[0].start
        assert text[amounts[0].start:amounts[0].end] == amounts[0].value


class TestVerifyAgainstSources:
    """verify_against_sources 方法测试。"""

    def test_all_verified(self) -> None:
        """所有高风险信息在来源中找到匹配。"""
        detector = HighRiskDetector()
        answer = "合同金额 50000元，签订日期 2024-01-15"
        sources = [
            {"content": "合同金额为 50000元，签订日期 2024-01-15，双方签字生效"},
        ]

        result = detector.verify_against_sources(answer, sources)

        assert result.has_risk is False
        assert result.unverified_count == 0
        assert result.action == "pass"

    def test_unverified_amount(self) -> None:
        """金额未在来源中找到匹配。"""
        detector = HighRiskDetector()
        answer = "合同金额 99999元"
        sources = [
            {"content": "合同金额为 50000元"},  # 金额不同
        ]

        result = detector.verify_against_sources(answer, sources)

        assert result.has_risk is True
        assert result.unverified_count > 0
        assert result.action in ("warn", "block")

    def test_no_sources(self) -> None:
        """无来源文档时所有高风险信息都无法核验。"""
        detector = HighRiskDetector()
        answer = "合同金额 50000元"
        result = detector.verify_against_sources(answer, [])

        assert result.has_risk is True
        assert result.action == "warn"
        assert result.unverified_count > 0

    def test_no_high_risk_info(self) -> None:
        """答案中无高风险信息时返回 pass。"""
        detector = HighRiskDetector()
        answer = "这是一个普通的帮助说明。"
        sources = [{"content": "普通帮助文档"}]

        result = detector.verify_against_sources(answer, sources)

        assert result.has_risk is False
        assert result.action == "pass"
        assert result.total_count == 0

    def test_high_unverified_ratio_blocks(self) -> None:
        """未核验比例过高时建议阻断。"""
        detector = HighRiskDetector()
        answer = "金额 99999元，日期 2099-12-31，合同条款，违约赔偿"
        sources = [{"content": "无关内容"}]

        result = detector.verify_against_sources(answer, sources)

        assert result.has_risk is True
        assert result.action == "block"

    def test_partial_verification_warns(self) -> None:
        """部分未核验时建议警告。"""
        detector = HighRiskDetector()
        answer = "金额 50000元，日期 2099-12-31"  # 金额匹配，日期不匹配
        sources = [{"content": "合同金额为 50000元"}]

        result = detector.verify_against_sources(answer, sources)

        assert result.has_risk is True
        assert result.action == "warn"

    def test_verified_item_has_source_snippet(self) -> None:
        """已核验项包含来源片段。"""
        detector = HighRiskDetector()
        answer = "金额 50000元"
        sources = [{"content": "合同金额为 50000元，已支付"}]

        result = detector.verify_against_sources(answer, sources)

        verified_items = [i for i in result.items if i.verified]
        assert len(verified_items) > 0
        assert verified_items[0].source_snippet != ""

    def test_result_to_dict(self) -> None:
        """to_dict 返回正确字典。"""
        result = HighRiskResult(
            items=[HighRiskItem(type="amount", value="100元", start=0, end=4)],
            unverified_count=1,
            has_risk=True,
            action="warn",
        )
        d = result.to_dict()
        assert d["total_count"] == 1
        assert d["unverified_count"] == 1
        assert d["has_risk"] is True
        assert d["action"] == "warn"
        assert len(d["items"]) == 1

    def test_total_count_property(self) -> None:
        """total_count 属性正确。"""
        result = HighRiskResult(
            items=[
                HighRiskItem(type="amount", value="100元", start=0, end=4),
                HighRiskItem(type="date", value="2024-01-01", start=5, end=15),
            ],
        )
        assert result.total_count == 2

    def test_item_to_dict(self) -> None:
        """HighRiskItem.to_dict 正确。"""
        item = HighRiskItem(
            type="amount",
            value="50000元",
            start=10,
            end=17,
            verified=True,
            source_snippet="金额为 50000元 已确认",
        )
        d = item.to_dict()
        assert d["type"] == "amount"
        assert d["value"] == "50000元"
        assert d["verified"] is True
        assert d["source_snippet"] == "金额为 50000元 已确认"
