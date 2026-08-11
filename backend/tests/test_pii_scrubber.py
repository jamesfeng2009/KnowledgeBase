"""
PII Scrubber 测试 — 覆盖脱敏正则、递归结构、span 批处理与 LangFuse 接入。

测试范围：
    1. PIIScrubber.scrub_text — 四种 PII 模式识别（手机/身份证/邮箱/银行卡）
       和替换顺序（身份证优先于银行卡，避免 18 位身份证被 16-19 位银行卡吞并）
    2. PIIScrubber.scrub_value — 递归 dict/list/tuple 处理
    3. PIIScrubber.scrub_span_io — span 三字段批处理
    4. enabled 开关关闭时原样返回
    5. get_stats / reset 统计准确性
    6. get_default_scrubber 单例与 config 读取
    7. langfuse_tracer.py 三处接入点（end_span / span / finalize）
       验证 LangFuse SDK 和本地 SpanRecord 都收到脱敏后数据
"""

from __future__ import annotations

import sys
from unittest.mock import MagicMock

import pytest

# Mock celery（测试环境未安装，参考 test_span_record.py）
if "celery" not in sys.modules:
    mock_celery = MagicMock()
    mock_celery.Celery = MagicMock
    sys.modules["celery"] = mock_celery
if "celery_app" not in sys.modules:
    mock_celery_app = MagicMock()
    mock_celery_app.celery_app = MagicMock()
    sys.modules["celery_app"] = mock_celery_app


# ======================================================================
# PIIScrubber.scrub_text — 四种 PII 模式识别
# ======================================================================


class TestScrubTextPhone:
    """手机号脱敏。"""

    def test_basic_phone(self) -> None:
        from app.observability.pii_scrubber import PIIScrubber

        s = PIIScrubber()
        assert s.scrub_text("call 13800138000 now") == "call [PHONE] now"

    def test_phone_at_start(self) -> None:
        from app.observability.pii_scrubber import PIIScrubber

        s = PIIScrubber()
        assert s.scrub_text("13800138000 is my number") == "[PHONE] is my number"

    def test_phone_at_end(self) -> None:
        from app.observability.pii_scrubber import PIIScrubber

        s = PIIScrubber()
        assert s.scrub_text("my number 13800138000") == "my number [PHONE]"

    def test_multiple_phones(self) -> None:
        from app.observability.pii_scrubber import PIIScrubber

        s = PIIScrubber()
        result = s.scrub_text("13800138000 and 13900139000")
        assert result == "[PHONE] and [PHONE]"

    def test_phone_in_chinese_context(self) -> None:
        from app.observability.pii_scrubber import PIIScrubber

        s = PIIScrubber()
        assert s.scrub_text("联系电话13800138000已记录") == "联系电话[PHONE]已记录"

    def test_phone_with_boundary_digits_not_matched(self) -> None:
        """边界断言 — 长数字串内部不应误命中手机号。"""
        from app.observability.pii_scrubber import PIIScrubber

        s = PIIScrubber()
        # 20 位数字串中间的 11 位不应被识别为手机号（前后是数字）
        result = s.scrub_text("12345678901234567890")
        # 整串是 20 位，被银行卡吞并（16-19 位匹配），不含手机号
        assert "[PHONE]" not in result

    def test_phone_starting_not_1_not_matched(self) -> None:
        """非 1 开头的 11 位数字不是手机号。"""
        from app.observability.pii_scrubber import PIIScrubber

        s = PIIScrubber()
        # 2 开头不被识别为手机号；但 11 位数字前后无数字，也不命中银行卡（< 16 位）
        result = s.scrub_text("23456789012")
        assert "[PHONE]" not in result

    def test_phone_second_digit_invalid_not_matched(self) -> None:
        """第二位不在 3-9 范围的 11 位号码不识别（如 110...）。"""
        from app.observability.pii_scrubber import PIIScrubber

        s = PIIScrubber()
        # 110 开头（第二位 1）不匹配手机号正则
        result = s.scrub_text("11012345678")
        assert "[PHONE]" not in result


class TestScrubTextIdCard:
    """身份证脱敏。"""

    def test_basic_idcard_numeric(self) -> None:
        """18 位全数字身份证（17 位 + 数字校验位）。"""
        from app.observability.pii_scrubber import PIIScrubber

        s = PIIScrubber()
        assert s.scrub_text("id 110101199003070019 end") == "id [IDCARD] end"

    def test_basic_idcard_with_x(self) -> None:
        """17 位数字 + X 结尾的身份证。"""
        from app.observability.pii_scrubber import PIIScrubber

        s = PIIScrubber()
        assert s.scrub_text("id 11010119900307001X end") == "id [IDCARD] end"

    def test_basic_idcard_with_lowercase_x(self) -> None:
        """小写 x 结尾的身份证。"""
        from app.observability.pii_scrubber import PIIScrubber

        s = PIIScrubber()
        assert s.scrub_text("id 11010119900307001x end") == "id [IDCARD] end"

    def test_idcard_priority_over_bankcard(self) -> None:
        """身份证必须先于银行卡替换 — 否则 18 位身份证被银行卡吞并。"""
        from app.observability.pii_scrubber import PIIScrubber

        s = PIIScrubber()
        result = s.scrub_text("id=110101199003070019")
        assert result == "id=[IDCARD]"
        assert "[BANKCARD]" not in result

    def test_idcard_at_start(self) -> None:
        from app.observability.pii_scrubber import PIIScrubber

        s = PIIScrubber()
        assert s.scrub_text("110101199003070019 is id") == "[IDCARD] is id"

    def test_idcard_at_end(self) -> None:
        from app.observability.pii_scrubber import PIIScrubber

        s = PIIScrubber()
        assert s.scrub_text("id is 110101199003070019") == "id is [IDCARD]"

    def test_idcard_in_chinese_context(self) -> None:
        from app.observability.pii_scrubber import PIIScrubber

        s = PIIScrubber()
        assert s.scrub_text("身份证号110101199003070019已记录") == "身份证号[IDCARD]已记录"

    def test_idcard_boundary_letter_not_matched(self) -> None:
        """身份证前后是字母时不命中（避免吞并长字母数字串）。"""
        from app.observability.pii_scrubber import PIIScrubber

        s = PIIScrubber()
        # 前面是字母 A，后向断言失败
        result = s.scrub_text("A110101199003070019")
        # 但 bankcard 正则也不匹配字母开头的，整串原样返回
        assert "[IDCARD]" not in result


class TestScrubTextEmail:
    """邮箱脱敏。"""

    def test_basic_email(self) -> None:
        from app.observability.pii_scrubber import PIIScrubber

        s = PIIScrubber()
        assert s.scrub_text("mail a@b.com") == "mail [EMAIL]"

    def test_complex_email(self) -> None:
        from app.observability.pii_scrubber import PIIScrubber

        s = PIIScrubber()
        assert s.scrub_text("contact: john.doe+filter@example.co.uk") == "contact: [EMAIL]"

    def test_email_at_start(self) -> None:
        from app.observability.pii_scrubber import PIIScrubber

        s = PIIScrubber()
        assert s.scrub_text("a@b.com is mail") == "[EMAIL] is mail"

    def test_email_at_end(self) -> None:
        from app.observability.pii_scrubber import PIIScrubber

        s = PIIScrubber()
        assert s.scrub_text("mail is a@b.com") == "mail is [EMAIL]"

    def test_multiple_emails(self) -> None:
        from app.observability.pii_scrubber import PIIScrubber

        s = PIIScrubber()
        result = s.scrub_text("a@b.com and c@d.com")
        assert result == "[EMAIL] and [EMAIL]"

    def test_email_with_subdomain(self) -> None:
        from app.observability.pii_scrubber import PIIScrubber

        s = PIIScrubber()
        assert s.scrub_text("user@mail.sub.com ok") == "[EMAIL] ok"

    def test_short_tld_not_matched(self) -> None:
        """TLD 必须至少 2 位字母 — 单字符 TLD 不识别。"""
        from app.observability.pii_scrubber import PIIScrubber

        s = PIIScrubber()
        # a@b.c 不匹配（TLD 仅 1 位）
        result = s.scrub_text("a@b.c")
        assert "[EMAIL]" not in result


class TestScrubTextBankCard:
    """银行卡脱敏。"""

    def test_basic_16_digit_bankcard(self) -> None:
        from app.observability.pii_scrubber import PIIScrubber

        s = PIIScrubber()
        assert s.scrub_text("card 6222020200011111 end") == "card [BANKCARD] end"

    def test_19_digit_bankcard(self) -> None:
        from app.observability.pii_scrubber import PIIScrubber

        s = PIIScrubber()
        assert s.scrub_text("card 6222020200011111111 end") == "card [BANKCARD] end"

    def test_bankcard_at_start(self) -> None:
        from app.observability.pii_scrubber import PIIScrubber

        s = PIIScrubber()
        assert s.scrub_text("6222020200011111 card") == "[BANKCARD] card"

    def test_bankcard_at_end(self) -> None:
        from app.observability.pii_scrubber import PIIScrubber

        s = PIIScrubber()
        assert s.scrub_text("card 6222020200011111") == "card [BANKCARD]"

    def test_bankcard_in_chinese_context(self) -> None:
        from app.observability.pii_scrubber import PIIScrubber

        s = PIIScrubber()
        assert s.scrub_text("卡号6222020200011111已绑定") == "卡号[BANKCARD]已绑定"

    def test_15_digit_not_bankcard(self) -> None:
        """15 位数字不够银行卡最小长度 16。"""
        from app.observability.pii_scrubber import PIIScrubber

        s = PIIScrubber()
        result = s.scrub_text("num 622202020001111 end")
        # 15 位不被银行卡正则匹配；也不匹配手机号（不以 1 开头 + 第二位 3-9）
        assert "[BANKCARD]" not in result


# ======================================================================
# 混合 PII 场景
# ======================================================================


class TestScrubTextMixed:
    """多种 PII 同时出现的混合场景。"""

    def test_all_four_types_in_one_text(self) -> None:
        from app.observability.pii_scrubber import PIIScrubber

        s = PIIScrubber()
        text = "id=110101199003070019 bank=6222020200011111 phone=13800138000 mail=a@b.com"
        result = s.scrub_text(text)
        assert result == "id=[IDCARD] bank=[BANKCARD] phone=[PHONE] mail=[EMAIL]"

    def test_text_without_pii_unchanged(self) -> None:
        from app.observability.pii_scrubber import PIIScrubber

        s = PIIScrubber()
        text = "这是一段普通文本，不含任何 PII 信息。"
        assert s.scrub_text(text) == text

    def test_empty_string(self) -> None:
        from app.observability.pii_scrubber import PIIScrubber

        s = PIIScrubber()
        assert s.scrub_text("") == ""

    def test_none_handled_by_caller(self) -> None:
        """scrub_text(None) 不在 API 范围内 — 由 scrub_value 处理 None。

        scrub_text 直接调用时传 None 会被 `if not text` 拦截原样返回。
        """
        from app.observability.pii_scrubber import PIIScrubber

        s = PIIScrubber()
        # not None → True，原样返回
        assert s.scrub_text(None) is None  # type: ignore[arg-type]


# ======================================================================
# PIIScrubber.scrub_value — 递归结构
# ======================================================================


class TestScrubValue:
    """递归 scrub 任意嵌套结构。"""

    def test_string_value(self) -> None:
        from app.observability.pii_scrubber import PIIScrubber

        s = PIIScrubber()
        assert s.scrub_value("call 13800138000") == "call [PHONE]"

    def test_dict_flat(self) -> None:
        from app.observability.pii_scrubber import PIIScrubber

        s = PIIScrubber()
        result = s.scrub_value({"user": "a@b.com", "phone": "13800138000"})
        assert result == {"user": "[EMAIL]", "phone": "[PHONE]"}

    def test_dict_nested(self) -> None:
        from app.observability.pii_scrubber import PIIScrubber

        s = PIIScrubber()
        result = s.scrub_value({
            "outer": {"inner": "id 110101199003070019"},
            "list": ["mail a@b.com", "plain text"],
        })
        assert result == {
            "outer": {"inner": "id [IDCARD]"},
            "list": ["mail [EMAIL]", "plain text"],
        }

    def test_list_flat(self) -> None:
        from app.observability.pii_scrubber import PIIScrubber

        s = PIIScrubber()
        result = s.scrub_value(["13800138000", "a@b.com", "no pii"])
        assert result == ["[PHONE]", "[EMAIL]", "no pii"]

    def test_list_nested(self) -> None:
        from app.observability.pii_scrubber import PIIScrubber

        s = PIIScrubber()
        result = s.scrub_value([{"k": "13800138000"}, ["a@b.com"]])
        assert result == [{"k": "[PHONE]"}, ["[EMAIL]"]]

    def test_tuple_converted_to_list(self) -> None:
        """tuple 不可变，递归后转为 list。"""
        from app.observability.pii_scrubber import PIIScrubber

        s = PIIScrubber()
        result = s.scrub_value(("13800138000", "a@b.com"))
        assert result == ["[PHONE]", "[EMAIL]"]
        assert isinstance(result, list)

    def test_int_unchanged(self) -> None:
        from app.observability.pii_scrubber import PIIScrubber

        s = PIIScrubber()
        assert s.scrub_value(12345) == 12345

    def test_float_unchanged(self) -> None:
        from app.observability.pii_scrubber import PIIScrubber

        s = PIIScrubber()
        assert s.scrub_value(3.14) == 3.14

    def test_bool_unchanged(self) -> None:
        from app.observability.pii_scrubber import PIIScrubber

        s = PIIScrubber()
        assert s.scrub_value(True) is True
        assert s.scrub_value(False) is False

    def test_none_unchanged(self) -> None:
        from app.observability.pii_scrubber import PIIScrubber

        s = PIIScrubber()
        assert s.scrub_value(None) is None

    def test_dict_key_not_scrubbed(self) -> None:
        """dict 的 key 不脱敏（key 是字段名，不含 PII）。"""
        from app.observability.pii_scrubber import PIIScrubber

        s = PIIScrubber()
        result = s.scrub_value({"13800138000": "value"})
        # key 原样保留，只有 value 脱敏
        assert "13800138000" in result
        assert result["13800138000"] == "value"

    def test_deeply_nested(self) -> None:
        """深层嵌套也能递归到底。"""
        from app.observability.pii_scrubber import PIIScrubber

        s = PIIScrubber()
        result = s.scrub_value({
            "a": {"b": {"c": {"d": "13800138000"}}},
        })
        assert result == {"a": {"b": {"c": {"d": "[PHONE]"}}}}

    def test_empty_structures(self) -> None:
        from app.observability.pii_scrubber import PIIScrubber

        s = PIIScrubber()
        assert s.scrub_value({}) == {}
        assert s.scrub_value([]) == []
        assert s.scrub_value(()) == []


# ======================================================================
# PIIScrubber.scrub_span_io — span 三字段批处理
# ======================================================================


class TestScrubSpanIO:
    """span input/output/metadata 三字段批处理。"""

    def test_all_three_fields_scrubbed(self) -> None:
        from app.observability.pii_scrubber import PIIScrubber

        s = PIIScrubber()
        i, o, m = s.scrub_span_io(
            input_data={"query": "my phone 13800138000"},
            output_data={"answer": "reply a@b.com"},
            metadata={"user": "zs@example.com"},
        )
        assert i == {"query": "my phone [PHONE]"}
        assert o == {"answer": "reply [EMAIL]"}
        assert m == {"user": "[EMAIL]"}

    def test_metadata_none_returns_empty_dict(self) -> None:
        from app.observability.pii_scrubber import PIIScrubber

        s = PIIScrubber()
        i, o, m = s.scrub_span_io("input", "output", None)
        assert i == "input"
        assert o == "output"
        assert m == {}

    def test_input_none(self) -> None:
        from app.observability.pii_scrubber import PIIScrubber

        s = PIIScrubber()
        i, o, m = s.scrub_span_io(None, "out 13800138000", {"k": "v"})
        assert i is None
        assert o == "out [PHONE]"
        assert m == {"k": "v"}

    def test_output_none(self) -> None:
        from app.observability.pii_scrubber import PIIScrubber

        s = PIIScrubber()
        i, o, m = s.scrub_span_io("in 13800138000", None, {"k": "v"})
        assert i == "in [PHONE]"
        assert o is None
        assert m == {"k": "v"}

    def test_returns_tuple_of_three(self) -> None:
        from app.observability.pii_scrubber import PIIScrubber

        s = PIIScrubber()
        result = s.scrub_span_io("a", "b", {"c": "d"})
        assert isinstance(result, tuple)
        assert len(result) == 3

    def test_increments_field_scrub_count(self) -> None:
        from app.observability.pii_scrubber import PIIScrubber

        s = PIIScrubber()
        s.scrub_span_io("a", "b", {})
        s.scrub_span_io("c", "d", {})
        stats = s.get_stats()
        assert stats["field_scrub_count"] == 2


# ======================================================================
# enabled 开关
# ======================================================================


class TestEnabledSwitch:
    """enabled=False 时所有操作原样返回。"""

    def test_scrub_text_disabled(self) -> None:
        from app.observability.pii_scrubber import PIIScrubber

        s = PIIScrubber(enabled=False)
        text = "13800138000"
        assert s.scrub_text(text) == text

    def test_scrub_value_disabled(self) -> None:
        from app.observability.pii_scrubber import PIIScrubber

        s = PIIScrubber(enabled=False)
        value = {"phone": "13800138000"}
        assert s.scrub_value(value) == value

    def test_scrub_span_io_disabled(self) -> None:
        from app.observability.pii_scrubber import PIIScrubber

        s = PIIScrubber(enabled=False)
        i, o, m = s.scrub_span_io("13800138000", "a@b.com", None)
        assert i == "13800138000"
        assert o == "a@b.com"
        assert m == {}

    def test_disabled_no_stats_increment(self) -> None:
        from app.observability.pii_scrubber import PIIScrubber

        s = PIIScrubber(enabled=False)
        s.scrub_text("13800138000")
        s.scrub_value({"k": "13800138000"})
        s.scrub_span_io("a", "b", {})
        stats = s.get_stats()
        assert stats["total_hits"] == 0
        # field_scrub_count 在 enabled=False 时不增加（scrub_span_io 直接返回）
        assert stats["field_scrub_count"] == 0

    def test_enabled_property(self) -> None:
        from app.observability.pii_scrubber import PIIScrubber

        assert PIIScrubber(enabled=True).enabled is True
        assert PIIScrubber(enabled=False).enabled is False


# ======================================================================
# 统计与重置
# ======================================================================


class TestStats:
    """get_stats / reset 准确性。"""

    def test_initial_stats(self) -> None:
        from app.observability.pii_scrubber import PIIScrubber

        s = PIIScrubber()
        stats = s.get_stats()
        assert stats["enabled"] is True
        assert stats["total_hits"] == 0
        assert stats["field_scrub_count"] == 0
        assert stats["hit_counts"] == {
            "idcard": 0,
            "bankcard": 0,
            "phone": 0,
            "email": 0,
        }

    def test_hit_counts_after_scrub(self) -> None:
        from app.observability.pii_scrubber import PIIScrubber

        s = PIIScrubber()
        s.scrub_text("13800138000 a@b.com 6222020200011111 110101199003070019")
        stats = s.get_stats()
        assert stats["hit_counts"]["phone"] == 1
        assert stats["hit_counts"]["email"] == 1
        assert stats["hit_counts"]["bankcard"] == 1
        assert stats["hit_counts"]["idcard"] == 1
        assert stats["total_hits"] == 4

    def test_reset(self) -> None:
        from app.observability.pii_scrubber import PIIScrubber

        s = PIIScrubber()
        s.scrub_text("13800138000")
        s.scrub_span_io("a", "b", {})
        assert s.get_stats()["total_hits"] > 0
        s.reset()
        stats = s.get_stats()
        assert stats["total_hits"] == 0
        assert stats["field_scrub_count"] == 0

    def test_cumulative_count_multiple_calls(self) -> None:
        from app.observability.pii_scrubber import PIIScrubber

        s = PIIScrubber()
        s.scrub_text("13800138000")
        s.scrub_text("13900139000")
        s.scrub_text("a@b.com")
        assert s.get_stats()["hit_counts"]["phone"] == 2
        assert s.get_stats()["hit_counts"]["email"] == 1


# ======================================================================
# get_default_scrubber 单例
# ======================================================================


class TestDefaultScrubber:
    """get_default_scrubber 单例与 config 读取。"""

    def teardown_method(self) -> None:
        """每个测试后重置单例，避免污染后续测试。"""
        from app.observability import pii_scrubber

        pii_scrubber.reset_default_scrubber()

    def test_returns_instance(self) -> None:
        from app.observability.pii_scrubber import PIIScrubber, get_default_scrubber

        scrubber = get_default_scrubber()
        assert isinstance(scrubber, PIIScrubber)

    def test_singleton_reused(self) -> None:
        from app.observability.pii_scrubber import get_default_scrubber

        s1 = get_default_scrubber()
        s2 = get_default_scrubber()
        assert s1 is s2

    def test_default_enabled(self) -> None:
        """默认配置下应启用（settings.LANGFUSE_PII_SCRUB_ENABLED 默认 True）。"""
        from app.observability.pii_scrubber import get_default_scrubber

        scrubber = get_default_scrubber()
        assert scrubber.enabled is True

    def test_config_disabled(self) -> None:
        """config 关闭时单例应禁用。"""
        from app.config import get_settings
        from app.observability import pii_scrubber

        pii_scrubber.reset_default_scrubber()
        # 修改 settings — get_settings 是 lru_cache，需先 cache_clear
        get_settings.cache_clear()
        original = None
        try:
            # 直接 patch settings 实例的属性
            settings = get_settings()
            original = settings.LANGFUSE_PII_SCRUB_ENABLED
            settings.LANGFUSE_PII_SCRUB_ENABLED = False
            pii_scrubber.reset_default_scrubber()
            scrubber = pii_scrubber.get_default_scrubber()
            assert scrubber.enabled is False
        finally:
            if original is not None:
                settings.LANGFUSE_PII_SCRUB_ENABLED = original
            get_settings.cache_clear()
            pii_scrubber.reset_default_scrubber()

    def test_reset_default_scrubber(self) -> None:
        from app.observability import pii_scrubber

        s1 = pii_scrubber.get_default_scrubber()
        pii_scrubber.reset_default_scrubber()
        s2 = pii_scrubber.get_default_scrubber()
        # 重置后是新实例
        assert s1 is not s2


# ======================================================================
# LangFuse 接入测试 — TraceContext 三处出口
# ======================================================================


class TestLangFuseEndSpanScrub:
    """TraceContext.end_span 在 LangFuse span export 前应用 scrubber。"""

    def teardown_method(self) -> None:
        from app.observability import pii_scrubber

        pii_scrubber.reset_default_scrubber()

    def test_end_span_scrubs_langfuse_input_output_metadata(self) -> None:
        """end_span 调用 LangFuse span 时收到的是脱敏后数据。"""
        from app.observability.langfuse_tracer import TraceContext

        # mock LangFuse trace
        mock_trace = MagicMock()
        mock_span = MagicMock()
        mock_trace.span.return_value = mock_span

        ctx = TraceContext()
        ctx._trace = mock_trace  # type: ignore[assignment]

        ctx.end_span(
            span_id=None,  # 跳过本地 recorder
            name="think_iter1",
            output_data={"answer": "phone 13800138000"},
            metadata={"user_input": "call 13800138000", "latency_ms": 100.0},
            langfuse_input={"query": "my phone 13800138000"},
        )

        # LangFuse span 收到的是脱敏后的数据
        mock_trace.span.assert_called_once()
        call_kwargs = mock_trace.span.call_args.kwargs
        assert call_kwargs["name"] == "think_iter1"
        assert call_kwargs["input"] == {"query": "my phone [PHONE]"}
        assert call_kwargs["output"] == {"answer": "phone [PHONE]"}
        assert call_kwargs["metadata"]["user_input"] == "call [PHONE]"
        assert call_kwargs["metadata"]["latency_ms"] == 100.0  # 非 PII 保留

    def test_end_span_scrubs_local_spanrecord(self) -> None:
        """本地 SpanRecord 也收到脱敏后数据。"""
        from app.observability.langfuse_tracer import TraceContext

        mock_recorder = MagicMock()
        mock_trace = MagicMock()

        ctx = TraceContext(recorder=mock_recorder)
        ctx._trace = mock_trace  # type: ignore[assignment]

        ctx.end_span(
            span_id="span-1",
            name="retrieve_iter1",
            output_data={"docs": "phone 13800138000"},
            metadata={"doc_count": 3, "user": "zs@example.com"},
            langfuse_input={"query": "mail a@b.com"},
        )

        # 本地 recorder 收到脱敏后的 output_ref 和 metadata
        mock_recorder.end_span.assert_called_once()
        call_kwargs = mock_recorder.end_span.call_args.kwargs
        assert "[PHONE]" in call_kwargs["output_ref"]
        assert call_kwargs["metadata"]["user"] == "[EMAIL]"
        assert call_kwargs["metadata"]["doc_count"] == 3  # 非 PII 保留


class TestLangFuseSpanScrub:
    """TraceContext.span 在 LangFuse span export 前应用 scrubber。"""

    def teardown_method(self) -> None:
        from app.observability import pii_scrubber

        pii_scrubber.reset_default_scrubber()

    def test_span_scrubs_all_three_fields(self) -> None:
        from app.observability.langfuse_tracer import TraceContext

        mock_trace = MagicMock()
        ctx = TraceContext()
        ctx._trace = mock_trace  # type: ignore[assignment]

        ctx.span(
            name="generate_iter1",
            input_data={"context": "phone 13800138000"},
            output_data={"answer": "mail a@b.com"},
            metadata={"tokens": 100, "user_input": "id 110101199003070019"},
        )

        mock_trace.span.assert_called_once()
        kwargs = mock_trace.span.call_args.kwargs
        assert kwargs["input"] == {"context": "phone [PHONE]"}
        assert kwargs["output"] == {"answer": "mail [EMAIL]"}
        assert kwargs["metadata"]["user_input"] == "id [IDCARD]"
        assert kwargs["metadata"]["tokens"] == 100

    def test_span_local_spanrecord_scrubbed(self) -> None:
        from app.observability.langfuse_tracer import TraceContext

        mock_recorder = MagicMock()
        ctx = TraceContext(recorder=mock_recorder)
        ctx._trace = None  # 不走 LangFuse 分支

        ctx.span(
            name="reflect_iter1",
            input_data="phone 13800138000",
            output_data="mail a@b.com",
            metadata={"tokens": 50},
        )

        mock_recorder.record_closed.assert_called_once()
        kwargs = mock_recorder.record_closed.call_args.kwargs
        assert "[PHONE]" in kwargs["input_ref"]
        assert "[EMAIL]" in kwargs["output_ref"]
        assert kwargs["metadata"]["tokens"] == 50

    def test_span_no_trace_no_recorder_no_crash(self) -> None:
        """无 LangFuse 无 recorder 时 span 不崩溃（scrubber 仍执行但结果丢弃）。"""
        from app.observability.langfuse_tracer import TraceContext

        ctx = TraceContext()
        ctx._trace = None  # type: ignore[assignment]
        # 不应抛异常
        ctx.span(
            name="think_iter1",
            input_data="phone 13800138000",
            output_data="ok",
            metadata={"k": "v"},
        )


class TestLangFuseFinalizeScrub:
    """TraceContext.finalize 在 LangFuse update 前应用 scrubber。"""

    def teardown_method(self) -> None:
        from app.observability import pii_scrubber

        pii_scrubber.reset_default_scrubber()

    def test_finalize_scrubs_output_and_metadata(self) -> None:
        from app.observability.langfuse_tracer import TraceContext

        mock_trace = MagicMock()
        ctx = TraceContext(metadata={"init_user": "zs@example.com"})
        ctx._trace = mock_trace  # type: ignore[assignment]
        ctx._spans = [MagicMock(), MagicMock()]  # 模拟两个 span

        ctx.finalize(
            output="answer: phone 13800138000",
            metadata={"final_user": "a@b.com", "span_count": 2},
        )

        mock_trace.update.assert_called_once()
        kwargs = mock_trace.update.call_args.kwargs
        assert "[PHONE]" in kwargs["output"]
        assert kwargs["metadata"]["final_user"] == "[EMAIL]"
        # init_user 来自 self.metadata，也脱敏
        assert kwargs["metadata"]["init_user"] == "[EMAIL]"
        # span_count 被覆盖为实际 span 数量
        assert kwargs["metadata"]["span_count"] == 2

    def test_finalize_scrubs_local_root_span(self) -> None:
        from app.observability.langfuse_tracer import TraceContext

        mock_recorder = MagicMock()
        ctx = TraceContext(recorder=mock_recorder)
        ctx._trace = None  # type: ignore[assignment]
        ctx._root_span_id = "root-1"

        ctx.finalize(
            output="phone 13800138000",
            metadata={"k": "v"},
        )

        mock_recorder.end_span.assert_called_once()
        kwargs = mock_recorder.end_span.call_args.kwargs
        assert "[PHONE]" in kwargs["output_ref"]
        assert kwargs["metadata"]["k"] == "v"
        # root_span_id 被清空（finally 块）
        assert ctx._root_span_id is None


# ======================================================================
# 端到端：trace_node 装饰器 + scrubber 集成
# ======================================================================


class TestTraceNodeIntegration:
    """trace_node 装饰器触发 end_span 时也应用 scrubber。"""

    def teardown_method(self) -> None:
        from app.observability import pii_scrubber

        pii_scrubber.reset_default_scrubber()

    @pytest.mark.asyncio
    async def test_trace_node_end_span_scrubs_pii(self) -> None:
        """trace_node 装饰的节点函数返回含 PII 内容时，end_span 会脱敏。"""
        from app.observability.langfuse_tracer import TraceContext, trace_node

        mock_trace = MagicMock()
        ctx = TraceContext()
        ctx._trace = mock_trace  # type: ignore[assignment]

        class FakeEngine:
            _trace_ctx: TraceContext | None = ctx

            @trace_node("think")
            async def _think(self, state: dict) -> str:
                # 返回含 PII 的内容
                return "answer: phone 13800138000"

        engine = FakeEngine()
        await engine._think({"iteration": 1, "session_id": "s1"})

        # end_span 触发了 LangFuse span，验证 PII 被脱敏
        assert mock_trace.span.called
        kwargs = mock_trace.span.call_args.kwargs
        # output_data 是 {"result_preview": "answer: phone [PHONE]"} 之类
        output_str = str(kwargs.get("output", ""))
        assert "[PHONE]" in output_str
        assert "13800138000" not in output_str


# ======================================================================
# 配置不污染
# ======================================================================


class TestConfigIsolation:
    """确保测试不污染全局 config 状态。"""

    def teardown_method(self) -> None:
        from app.observability import pii_scrubber

        pii_scrubber.reset_default_scrubber()

    def test_default_config_remains_true(self) -> None:
        """跑完所有测试后默认配置仍为 True。"""
        from app.config import get_settings

        get_settings.cache_clear()
        from app.observability.pii_scrubber import get_default_scrubber

        scrubber = get_default_scrubber()
        assert scrubber.enabled is True
