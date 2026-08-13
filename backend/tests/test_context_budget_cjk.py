"""
P1-5 CJK token 估算改进 — 测试套件。

验证 context_budget.py 的 CJK 感知 token 估算：
    1. CJK 字符识别（_is_cjk_char / _count_cjk_chars）
    2. CJK token 估算（estimate_tokens_for_text）
    3. 消息列表 token 估算（ContextBudgetManager.estimate_tokens）
    4. 中文高估策略验证（vs 旧版 3.5 系数）
    5. 中英混合文本估算
    6. 边界情况（空输入 / None content / 非 str content）
    7. 不影响现有 should_compress / compress 行为
"""

from __future__ import annotations

import pytest

from app.rag.context_budget import (
    _CJK_CHARS_PER_TOKEN,
    _CJK_RANGES,
    _NON_CJK_CHARS_PER_TOKEN,
    _count_cjk_chars,
    _is_cjk_char,
    estimate_tokens_for_text,
    ContextBudgetManager,
)


# ======================================================================
# 1. CJK 字符识别
# ======================================================================


class TestCjkCharDetection:
    """CJK 字符识别 — _is_cjk_char 测试。"""

    @pytest.mark.parametrize(
        "char,expected",
        [
            ("中", True),       # CJK 基本汉字 U+4E2D
            ("文", True),       # CJK 基本汉字 U+6587
            ("世", True),       # CJK 基本汉字 U+4E16
            ("界", True),       # CJK 基本汉字 U+754C
            ("あ", True),       # 平假名 U+3042
            ("カ", True),       # 片假名 U+30AB
            ("韓", True),       # 韩文汉字
        ],
    )
    def test_cjk_chars_detected(self, char: str, expected: bool):
        assert _is_cjk_char(char) == expected

    @pytest.mark.parametrize(
        "char,expected",
        [
            ("a", False),       # 半角英文
            ("A", False),       # 半角大写
            ("1", False),       # 半角数字
            (" ", False),       # 半角空格
            (".", False),       # 半角句号
            (",", False),       # 半角逗号
            (chr(0x0A), False), # 换行符 LF
            (chr(0x09), False), # 制表符 TAB
        ],
    )
    def test_non_cjk_chars_rejected(self, char: str, expected: bool):
        assert _is_cjk_char(char) == expected

    @pytest.mark.parametrize("cp", [0x3000, 0x3001, 0x3002, 0x303F])
    def test_cjk_symbols_and_punctuation(self, cp: int):
        """CJK 符号和标点（U+3000-U+303F）应识别为 CJK。"""
        assert _is_cjk_char(chr(cp)) is True

    @pytest.mark.parametrize("cp", [0xFF01, 0xFF1A, 0xFF21, 0xFF41])
    def test_fullwidth_forms(self, cp: int):
        """全角形式（U+FF00-U+FFEF）应识别为 CJK。"""
        assert _is_cjk_char(chr(cp)) is True

    def test_empty_char_returns_false(self):
        assert _is_cjk_char("") is False

    def test_cjk_extension_b_rare_chars(self):
        """CJK 扩展 B（U+20000-U+2A6DF）罕见汉字应识别为 CJK。"""
        # U+20000 𠀀 是 CJK 扩展 B 的第一个字符
        assert _is_cjk_char(chr(0x20000)) is True
        assert _is_cjk_char(chr(0x2A6DF)) is True


class TestCountCjkChars:
    """CJK 字符计数 — _count_cjk_chars 测试。"""

    def test_empty_string(self):
        assert _count_cjk_chars("") == 0

    def test_pure_cjk(self):
        assert _count_cjk_chars("你好世界") == 4

    def test_pure_non_cjk(self):
        assert _count_cjk_chars("Hello World 123") == 0

    def test_mixed(self):
        assert _count_cjk_chars("Hello 世界!") == 2

    def test_mixed_with_fullwidth(self):
        # 全角空格 + 中文 + 英文
        text = chr(0x3000) + "中文" + "abc"
        assert _count_cjk_chars(text) == 3  # 全角空格 + 2 个中文

    def test_multiline_text(self):
        text = "第一行\n第二行\nthird line"
        # 「第一行」=3 个 CJK，「第二行」=3 个 CJK，共 6 个；\n 和英文不计入
        assert _count_cjk_chars(text) == 6


# ======================================================================
# 2. CJK token 估算
# ======================================================================


class TestEstimateTokensForText:
    """CJK 感知 token 估算 — estimate_tokens_for_text 测试。"""

    def test_empty_string(self):
        assert estimate_tokens_for_text("") == 0

    def test_pure_cjk_uses_cjk_coefficient(self):
        """纯 CJK 文本按 1.5 字符/token 估算。"""
        # 6 个 CJK 字符 → 6 / 1.5 = 4 token
        assert estimate_tokens_for_text("你好世界测试") == 4

    def test_pure_non_cjk_uses_english_coefficient(self):
        """纯英文按 4.0 字符/token 估算。"""
        # 8 个英文字符 → 8 / 4 = 2 token
        assert estimate_tokens_for_text("abcdefgh") == 2

    def test_mixed_text_uses_both_coefficients(self):
        """混合文本按字符类别分别估算。"""
        # 2 个中文 + 4 个英文 → 2/1.5 + 4/4 = 1.33 + 1 = 2.33 → int() = 2
        assert estimate_tokens_for_text("你好abcd") == 2

    def test_cjk_overestimates_vs_old_coefficient(self):
        """P1-5 核心目标：中文估算比旧版 3.5 系数高估。"""
        text = "中" * 1000  # 1000 个中文字符
        new_estimate = estimate_tokens_for_text(text)
        old_estimate = int(1000 / 3.5)  # 旧估算 ≈ 285
        # 新估算应明显大于旧估算（约 666 vs 285）
        assert new_estimate > old_estimate * 2, (
            f"中文应被高估: 新={new_estimate}, 旧={old_estimate}"
        )

    def test_english_slightly_lower_vs_old_coefficient(self):
        """英文估算略低于旧版（4.0 vs 3.5，更接近真实）。"""
        text = "a" * 1000
        new_estimate = estimate_tokens_for_text(text)
        old_estimate = int(1000 / 3.5)  # ≈ 285
        assert new_estimate == 250  # 1000/4 = 250
        assert new_estimate < old_estimate

    def test_cjk_coefficient_value(self):
        """CJK 系数为 1.5（每个 CJK 字符 ≈ 0.67 token）。"""
        assert _CJK_CHARS_PER_TOKEN == 1.5

    def test_non_cjk_coefficient_value(self):
        """非 CJK 系数为 4.0（4 个英文/数字字符 ≈ 1 token）。"""
        assert _NON_CJK_CHARS_PER_TOKEN == 4.0

    def test_returns_integer(self):
        """结果始终为整数。"""
        assert isinstance(estimate_tokens_for_text("test"), int)
        assert isinstance(estimate_tokens_for_text("测试"), int)
        assert isinstance(estimate_tokens_for_text("mixed 混合"), int)

    def test_single_cjk_char(self):
        """单个 CJK 字符 → 1/1.5 = 0.67 → int() = 0"""
        # 注意：int(0.667) = 0，单字符估算可能为 0
        # 但在消息列表中累计后不为 0
        result = estimate_tokens_for_text("中")
        assert result == 0  # 1/1.5 = 0.67 → int = 0

    def test_two_cjk_chars(self):
        """两个 CJK 字符 → 2/1.5 = 1.33 → int() = 1"""
        assert estimate_tokens_for_text("中文") == 1

    def test_three_cjk_chars(self):
        """三个 CJK 字符 → 3/1.5 = 2 token"""
        assert estimate_tokens_for_text("中文测") == 2


# ======================================================================
# 3. 消息列表 token 估算
# ======================================================================


class TestEstimateTokensMessages:
    """ContextBudgetManager.estimate_tokens — 消息列表估算。"""

    def test_empty_messages(self):
        assert ContextBudgetManager.estimate_tokens([]) == 0

    def test_single_message_pure_cjk(self):
        msgs = [{"role": "user", "content": "你好世界"}]
        # 4 个 CJK / 1.5 = 2.67 → int = 2
        assert ContextBudgetManager.estimate_tokens(msgs) == 2

    def test_single_message_pure_english(self):
        msgs = [{"role": "user", "content": "abcdefgh"}]
        # 8 / 4 = 2
        assert ContextBudgetManager.estimate_tokens(msgs) == 2

    def test_multiple_messages_accumulate(self):
        msgs = [
            {"role": "system", "content": "你是助手"},  # 4 CJK → 2 token
            {"role": "user", "content": "Hello"},      # 5 英文 → 1 token
            {"role": "assistant", "content": "好的"},   # 2 CJK → 1 token
        ]
        # 总计: 2 + 1 + 1 = 4
        assert ContextBudgetManager.estimate_tokens(msgs) == 4

    def test_message_without_content(self):
        """无 content 字段的消息视为 0 token。"""
        msgs = [{"role": "user"}]
        assert ContextBudgetManager.estimate_tokens(msgs) == 0

    def test_message_with_empty_content(self):
        msgs = [{"role": "user", "content": ""}]
        assert ContextBudgetManager.estimate_tokens(msgs) == 0

    def test_message_with_none_content(self):
        """content=None 不崩溃，视为 0 token。"""
        msgs = [{"role": "user", "content": None}]
        assert ContextBudgetManager.estimate_tokens(msgs) == 0

    def test_message_with_non_string_content_falls_back_gracefully(self):
        """content 为 list/dict 时降级为字符串估算，不崩溃。"""
        msgs_list = [{"role": "user", "content": ["你好", "世界"]}]
        # str(["你好", "世界"]) = "['你好', '世界']" 含 4 个 CJK
        result = ContextBudgetManager.estimate_tokens(msgs_list)
        assert isinstance(result, int)
        assert result > 0  # 至少有些 token

        msgs_dict = [{"role": "user", "content": {"text": "你好"}}]
        result2 = ContextBudgetManager.estimate_tokens(msgs_dict)
        assert isinstance(result2, int)


# ======================================================================
# 4. 与现有 should_compress / compress 行为兼容性
# ======================================================================


class TestShouldCompressIntegration:
    """CJK 估算与 should_compress 集成测试。"""

    def test_short_messages_no_compress(self):
        """短消息不触发压缩。"""
        mgr = ContextBudgetManager(max_tokens=100)
        msgs = [
            {"role": "system", "content": "你是助手"},
            {"role": "user", "content": "你好"},
        ]
        # 只有 2 条消息，不满足压缩条件（需 > 2 + keep_recent=2 = 4）
        assert mgr.should_compress(msgs) is False

    def test_long_cjk_messages_trigger_compress_earlier(self):
        """P1-5 关键: 中文长消息更早触发压缩（vs 旧估算）。"""
        # 构造 5 条中文消息，每条 50 字
        # 5 条 × 50 字 = 250 个 CJK / 1.5 = 166 token
        # 旧估算: 250 / 3.5 = 71 token
        # 设 max_tokens=100，新版应触发压缩，旧版不会
        mgr = ContextBudgetManager(max_tokens=100, keep_recent=2)
        msgs = [
            {"role": "system", "content": "你是助手"},
            {"role": "user", "content": "用户问题"},
            {"role": "assistant", "content": "中" * 50},
            {"role": "assistant", "content": "中" * 50},
            {"role": "assistant", "content": "中" * 50},
        ]
        assert mgr.should_compress(msgs) is True, (
            "新版 CJK 估算应触发压缩（中文高估）"
        )

    def test_compress_still_works(self):
        """compress 方法在 CJK 估算下仍正常工作。

        需要足够多的中间消息才能让压缩真正减少消息数：
        原 7 条 → head(2) + summary(1) + tail(2) = 5 条。
        """
        mgr = ContextBudgetManager(max_tokens=40, keep_recent=2)
        msgs = [
            {"role": "system", "content": "你是助手"},
            {"role": "user", "content": "用户问题"},
            {"role": "assistant", "content": "中" * 50},  # 中间消息1，33 token
            {"role": "assistant", "content": "中" * 50},  # 中间消息2，33 token
            {"role": "assistant", "content": "中" * 50},  # 中间消息3，33 token
            {"role": "assistant", "content": "最近回复1"},  # tail
            {"role": "assistant", "content": "最近回复2"},  # tail
        ]
        result = mgr.compress(msgs)
        # 7 条 → 5 条（head 2 + summary 1 + tail 2）
        assert len(result) < len(msgs)
        assert len(result) == 5
        # 头尾保留
        assert result[0]["content"] == "你是助手"
        assert result[-1]["content"] == "最近回复2"
        # 中间被压缩为单条摘要
        assert result[2]["role"] == "user"
        assert "早期上下文摘要" in result[2]["content"]


# ======================================================================
# 5. 边界情况
# ======================================================================


class TestEdgeCases:
    """边界情况测试。"""

    def test_cjk_ranges_sorted_ascending(self):
        """CJK Unicode 范围按升序排列（保证 _is_cjk_char 短路逻辑正确）。"""
        for i in range(len(_CJK_RANGES) - 1):
            lo_curr, hi_curr = _CJK_RANGES[i]
            lo_next, _ = _CJK_RANGES[i + 1]
            assert hi_curr < lo_next, (
                f"CJK 范围应无重叠且升序: {_CJK_RANGES[i]} vs {_CJK_RANGES[i+1]}"
            )

    def test_long_text_no_overflow(self):
        """超长文本不溢出（性能测试）。"""
        text = "中" * 100_000  # 10 万字
        result = estimate_tokens_for_text(text)
        assert result == int(100_000 / 1.5)  # 66666

    def test_only_punctuation(self):
        """纯标点文本（含 CJK 标点）正常估算。"""
        # 全角句号 × 3 = 3 个 CJK / 1.5 = 2 token
        text = chr(0x3002) * 3  # 。。。 
        assert estimate_tokens_for_text(text) == 2

    def test_whitespace_only(self):
        """纯空白（半角空格）正常估算。"""
        text = "    " * 10  # 40 个半角空格
        result = estimate_tokens_for_text(text)
        assert result == int(40 / 4.0)  # 10

    def test_mixed_with_emoji(self):
        """含 emoji 的文本正常估算（emoji 非 CJK，按 4.0 算）。"""
        text = "你好 😀"  # 2 CJK + 1 emoji + 1 空格 = 4 字符
        # CJK: 2/1.5 = 1.33
        # 非 CJK: 2/4 = 0.5 (emoji + 空格)
        # 总: 1.33 + 0.5 = 1.83 → int = 1
        result = estimate_tokens_for_text(text)
        assert result == 1
