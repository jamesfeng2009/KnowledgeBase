"""
P0-4 检索层时间新鲜度（recency）单元测试。

覆盖：
    - filter_by_validity_window：生效窗口硬过滤（未来生效/已失效/窗口内/缺失字段/非法格式）
    - apply_recency_boost：平局裁决（tie band 内新版优先、超出带宽顺序不变、
      缺失时间按最旧、半衰期衰减标注、入参不可变）
    - 新旧规范冲突场景：同分旧版 vs 新版 → 新版排前
"""

from datetime import UTC, datetime, timedelta

from app.rag.recency import apply_recency_boost, filter_by_validity_window

NOW = datetime(2026, 8, 4, 12, 0, 0, tzinfo=UTC)


def _doc(
    chunk_id: str,
    score: float,
    updated_at: str | None = None,
    effective_from: str | None = None,
    effective_to: str | None = None,
) -> dict:
    d: dict = {"chunk_id": chunk_id, "score": score, "content": f"content of {chunk_id}"}
    if updated_at is not None:
        d["updated_at"] = updated_at
    if effective_from is not None:
        d["effective_from"] = effective_from
    if effective_to is not None:
        d["effective_to"] = effective_to
    return d


def _iso(dt: datetime) -> str:
    return dt.isoformat()


# ============================================================
# filter_by_validity_window
# ============================================================

class TestFilterByValidityWindow:
    """生效窗口硬过滤。"""

    def test_empty_list(self):
        assert filter_by_validity_window([], now=NOW) == []

    def test_no_window_fields_kept(self):
        """未配置窗口的文档永久有效（向后兼容）。"""
        docs = [_doc("a", 0.9), _doc("b", 0.8)]
        kept = filter_by_validity_window(docs, now=NOW)
        assert [d["chunk_id"] for d in kept] == ["a", "b"]

    def test_future_effective_from_dropped(self):
        """尚未生效（effective_from > now）→ 过滤。"""
        docs = [
            _doc("future", 0.9, effective_from=_iso(NOW + timedelta(days=1))),
            _doc("current", 0.8),
        ]
        kept = filter_by_validity_window(docs, now=NOW)
        assert [d["chunk_id"] for d in kept] == ["current"]

    def test_past_effective_from_kept(self):
        docs = [_doc("a", 0.9, effective_from=_iso(NOW - timedelta(days=1)))]
        kept = filter_by_validity_window(docs, now=NOW)
        assert len(kept) == 1

    def test_past_effective_to_dropped(self):
        """已失效（effective_to < now）→ 过滤。"""
        docs = [
            _doc("expired", 0.9, effective_to=_iso(NOW - timedelta(days=1))),
            _doc("valid", 0.8),
        ]
        kept = filter_by_validity_window(docs, now=NOW)
        assert [d["chunk_id"] for d in kept] == ["valid"]

    def test_future_effective_to_kept(self):
        docs = [_doc("a", 0.9, effective_to=_iso(NOW + timedelta(days=30)))]
        kept = filter_by_validity_window(docs, now=NOW)
        assert len(kept) == 1

    def test_within_window_kept(self):
        """生效窗口覆盖当前时间 → 保留。"""
        docs = [
            _doc(
                "a",
                0.9,
                effective_from=_iso(NOW - timedelta(days=10)),
                effective_to=_iso(NOW + timedelta(days=10)),
            )
        ]
        kept = filter_by_validity_window(docs, now=NOW)
        assert len(kept) == 1

    def test_invalid_date_format_kept(self):
        """非法时间格式 → 视为永久有效（优雅降级，不影响召回）。"""
        docs = [_doc("a", 0.9, effective_from="not-a-date", effective_to="")]
        kept = filter_by_validity_window(docs, now=NOW)
        assert len(kept) == 1

    def test_z_suffix_iso_format(self):
        """Z 后缀 ISO 格式可解析。"""
        docs = [
            _doc("expired", 0.9, effective_to="2026-08-01T00:00:00Z"),
            _doc("valid", 0.8, effective_to="2026-12-31T00:00:00Z"),
        ]
        kept = filter_by_validity_window(docs, now=NOW)
        assert [d["chunk_id"] for d in kept] == ["valid"]


# ============================================================
# apply_recency_boost
# ============================================================

class TestApplyRecencyBoost:
    """平局裁决 — tie band 内按 updated_at 新鲜度排序。"""

    def test_empty_list(self):
        assert apply_recency_boost([], now=NOW) == []

    def test_tie_group_newer_first(self):
        """新旧规范冲突核心场景：分差在 tie band 内 → 新版排前。"""
        old_spec = _doc("old_spec", 0.90, updated_at=_iso(NOW - timedelta(days=365)))
        new_spec = _doc("new_spec", 0.89, updated_at=_iso(NOW - timedelta(days=1)))
        result = apply_recency_boost([old_spec, new_spec], tie_band=0.02, now=NOW)
        assert [d["chunk_id"] for d in result] == ["new_spec", "old_spec"]

    def test_beyond_tie_band_order_unchanged(self):
        """分差超过 tie band → 顺序不变（新鲜度不喧宾夺主）。"""
        old_spec = _doc("old_spec", 0.95, updated_at=_iso(NOW - timedelta(days=365)))
        new_spec = _doc("new_spec", 0.80, updated_at=_iso(NOW - timedelta(days=1)))
        result = apply_recency_boost([new_spec, old_spec], tie_band=0.02, now=NOW)
        assert [d["chunk_id"] for d in result] == ["old_spec", "new_spec"]

    def test_missing_updated_at_treated_as_oldest(self):
        """缺失 updated_at 按最旧处理，平局组内排最后。"""
        no_ts = _doc("no_ts", 0.90)
        new_doc = _doc("new", 0.895, updated_at=_iso(NOW - timedelta(days=3)))
        result = apply_recency_boost([no_ts, new_doc], tie_band=0.02, now=NOW)
        assert [d["chunk_id"] for d in result] == ["new", "no_ts"]

    def test_recency_boost_annotation(self):
        """每条结果标注半衰期衰减系数：now ≈ 1.0，半衰期年龄 ≈ 0.5。"""
        fresh = _doc("fresh", 0.9, updated_at=_iso(NOW))
        half_life_old = _doc("half", 0.9, updated_at=_iso(NOW - timedelta(days=180)))
        result = apply_recency_boost(
            [fresh, half_life_old], tie_band=0.0, half_life_days=180.0, now=NOW
        )
        by_id = {d["chunk_id"]: d for d in result}
        assert by_id["fresh"]["recency_boost"] == 1.0
        assert by_id["half"]["recency_boost"] == 0.5

    def test_missing_timestamp_annotation_zero(self):
        result = apply_recency_boost([_doc("a", 0.9)], now=NOW)
        assert result[0]["recency_boost"] == 0.0

    def test_input_not_mutated(self):
        """返回新列表，不改入参。"""
        docs = [
            _doc("b", 0.90, updated_at=_iso(NOW - timedelta(days=10))),
            _doc("a", 0.89, updated_at=_iso(NOW - timedelta(days=1))),
        ]
        snapshot = [dict(d) for d in docs]
        result = apply_recency_boost(docs, tie_band=0.02, now=NOW)
        assert result is not docs
        assert docs == snapshot  # 入参未被修改

    def test_chain_tie_groups(self):
        """链式平局：0.90 / 0.89 / 0.88 两两差 0.01 ≤ band → 同组按新鲜度排。"""
        docs = [
            _doc("oldest", 0.90, updated_at=_iso(NOW - timedelta(days=300))),
            _doc("newest", 0.89, updated_at=_iso(NOW - timedelta(days=1))),
            _doc("middle", 0.88, updated_at=_iso(NOW - timedelta(days=30))),
        ]
        result = apply_recency_boost(docs, tie_band=0.02, now=NOW)
        assert [d["chunk_id"] for d in result] == ["newest", "middle", "oldest"]

    def test_epoch_timestamp_supported(self):
        """epoch 秒时间戳可解析。"""
        docs = [
            _doc("old", 0.90, updated_at=(NOW - timedelta(days=365)).timestamp()),
            _doc("new", 0.89, updated_at=NOW.timestamp()),
        ]
        result = apply_recency_boost(docs, tie_band=0.02, now=NOW)
        assert [d["chunk_id"] for d in result] == ["new", "old"]
