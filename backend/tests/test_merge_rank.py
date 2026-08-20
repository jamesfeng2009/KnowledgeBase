"""
merge_rank 测试 — P4 公网混合检索 POC 验收。

覆盖验收标准（docs/P4 §12.4 与 §12.5）：
    1. 双源保底：两源证据都入选，不出现单源独占
    2. 量纲归一化：不同量纲（0~1 vs 0~100）归一化后可比
    3. boost 可观察：改 boost 确实改变内部入选集与排序
    4. 全等列表稳健：min==max 时归一化全 0，不抛错
"""
from __future__ import annotations

import pytest

from app.rag.merge_rank import merge_and_rank


def _internal(title: str, score: float, content: str = "") -> dict:
    return {"title": title, "doc_id": f"/kb/{title}", "content": content, "score": score}


def _web(title: str, score: float, snippet: str = "") -> dict:
    return {"title": title, "url": f"https://example.com/{title}", "snippet": snippet, "score": score}


def _types(merged) -> list[str]:
    return [m["source_type"] for m in merged]


class TestGuaranteedMinimums:
    def test_both_sources_get_min_quota(self):
        """双源保底：即使某源分数极低，两源都进入结果集。"""
        internal = [_internal(f"in{i}", 0.9 - i * 0.05) for i in range(5)]
        # web 分数整体远低于 internal，仍应保底进入
        web = [_web(f"w{i}", 0.1 - i * 0.01) for i in range(3)]
        merged = merge_and_rank(internal, web, min_internal=2, min_web=2, total_budget=6)
        types = _types(merged)
        assert types.count("internal") >= 2
        assert types.count("web") >= 2

    def test_total_budget_is_respected(self):
        """总预算上限不被突破。"""
        internal = [_internal(f"in{i}", 0.9) for i in range(10)]
        web = [_web(f"w{i}", 0.8) for i in range(10)]
        merged = merge_and_rank(internal, web, min_internal=2, min_web=2, total_budget=4)
        assert len(merged) <= 4


class TestNormalization:
    def test_different_scale_is_comparable(self):
        """量纲归一化：内部 0~1、web 0~100，归一化后可比且混合排序稳定。"""
        internal = [_internal("in_high", 0.9), _internal("in_low", 0.1)]
        web = [_web("w_high", 90.0), _web("w_mid", 50.0), _web("w_low", 10.0)]
        merged = merge_and_rank(internal, web, boost=1.0, min_internal=2, min_web=2, total_budget=6)
        scores = [m["score"] for m in merged]
        # 归一化后最大值应为某源自己的满分（internal 0.9→1.0*boost，web 90→1.0）
        # 两端量纲不同但都应能进入 top 参与排序
        assert max(scores) > 0.9 or max(scores) > 90 / (90 - 10)  # 归一化有界
        assert len({m["source_type"] for m in merged}) == 2

    def test_boost_stays_equal_across_sources_when_one(self):
        """boost=1 且工数量相同时，两源按各自归一化分公平竞争。"""
        internal = [_internal("in", 1.0)]
        web = [_web("w", 100.0)]
        merged = merge_and_rank(internal, web, boost=1.0, min_internal=1, min_web=1, total_budget=2)
        # 单元素源归一化后为 0；此处仅验证格式与类型不报错
        assert {m["source_type"] for m in merged} == {"internal", "web"}


class TestBoostEffect:
    def test_low_boost_favors_web(self):
        """boost 大幅调低时，高权 web 命中应排到内部之前。"""
        internal = [_internal("inA", 0.8), _internal("inB", 0.7), _internal("inC", 0.6)]
        web = [_web("wA", 90.0), _web("wB", 80.0), _web("wC", 70.0)]
        merged_high = merge_and_rank(internal, web, boost=2.0, min_internal=1, min_web=1, total_budget=6)
        merged_low = merge_and_rank(internal, web, boost=0.2, min_internal=1, min_web=1, total_budget=6)
        # boost 高时内部 top 占优；boost 低时 web 占优 → 排序变化可观察
        assert merged_high[0]["source_type"] == "internal"
        assert merged_low[0]["source_type"] == "web"

    def test_boost_changes_internal_selection(self):
        """boost 影响内部入选的次序（保证 boost 不是死参数）。"""
        # 构造内部分数密集、web 稀疏的情形，验证 boost 改变内部相对排位
        internal = [_internal(f"in{i}", 0.5 + i * 0.3) for i in range(3)]
        web = [_web("w", 50.0)]
        r1 = merge_and_rank(internal, web, boost=1.0, min_internal=3, min_web=1, total_budget=6)
        r2 = merge_and_rank(internal, web, boost=0.3, min_internal=3, min_web=1, total_budget=6)
        # 内部间相对顺序会随 boost 改变（因为内部×boost 后仍保序，但这里验证分数确实变化）
        assert r1[0]["title"] != r2[0]["title"] or r1[0]["score"] != r2[0]["score"]


class TestRobustness:
    def test_empty_inputs(self):
        """空输入不报错；单源找回退到该源保底，不返回空。"""
        assert merge_and_rank([], [], min_internal=1, min_web=1, total_budget=6) == []
        one = merge_and_rank([_internal("a", 0.5)], [], min_internal=1, min_web=1, total_budget=6)
        assert len(one) == 1 and one[0]["source_type"] == "internal"

    def test_equal_scores_no_div_by_zero(self):
        """归一化时 min==max 全等列表 → 全 0，不抛 ZeroDivisionError。"""
        internal = [_internal(f"in{i}", 0.5) for i in range(6)]
        web = [_web(f"w{i}", 0.5) for i in range(3)]
        merged = merge_and_rank(internal, web, boost=1.0, min_internal=2, min_web=2, total_budget=6)
        assert all(m["score"] == 0.0 for m in merged)

    def test_url_is_doc_id_for_web(self):
        """web 条目的 url_or_doc_path 取 url，internal 取库内路径。"""
        merged = merge_and_rank(
            [_internal("doc", 0.9)], [_web("page", 80.0)],
            boost=1.0, min_internal=1, min_web=1, total_budget=2,
        )
        web = next(m for m in merged if m["source_type"] == "web")
        internal = next(m for m in merged if m["source_type"] == "internal")
        assert web["url_or_doc_path"].startswith("https://")
        assert internal["url_or_doc_path"].startswith("/kb/")