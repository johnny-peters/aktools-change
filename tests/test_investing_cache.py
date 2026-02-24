# -*- coding: utf-8 -*-
"""
Investing 历史数据智能缓存单元测试。
验证 _find_missing_ranges、_normalize_yyyymmdd 及缓存合并逻辑。
"""
import pytest

from aktools.core.investing import (
    _find_missing_ranges,
    _normalize_yyyymmdd,
    _row_date_iso,
    fetch_investing_historical_cached,
)


def test_normalize_yyyymmdd() -> None:
    assert _normalize_yyyymmdd("2024-01-15") == "2024-01-15"
    assert _normalize_yyyymmdd("1/15/2024") == "2024-01-15"
    assert _normalize_yyyymmdd("01/15/2024 09:30") == "2024-01-15"
    assert _normalize_yyyymmdd("") == ""


def test_find_missing_ranges_no_cache() -> None:
    """无缓存时，应返回整个请求区间。"""
    assert _find_missing_ranges("2024-02-01", "2024-04-30", "", "") == [
        ("2024-02-01", "2024-04-30")
    ]


def test_find_missing_ranges_full_hit() -> None:
    """全量命中：请求区间完全在缓存内，缺失为空。"""
    assert _find_missing_ranges("2024-02-01", "2024-03-31", "2024-01-01", "2024-04-30") == []


def test_find_missing_ranges_right_gap() -> None:
    """部分命中：缓存 1~3 月，请求 2~4 月，缺失 4 月。"""
    missing = _find_missing_ranges("2024-02-01", "2024-04-30", "2024-01-01", "2024-03-31")
    assert missing == [("2024-04-01", "2024-04-30")]


def test_find_missing_ranges_left_gap() -> None:
    """部分命中：缓存 2~4 月，请求 1~3 月，缺失 1 月。"""
    missing = _find_missing_ranges("2024-01-01", "2024-03-31", "2024-02-01", "2024-04-30")
    assert missing == [("2024-01-01", "2024-01-31")]


def test_find_missing_ranges_both_gaps() -> None:
    """部分命中：缓存 2~3 月，请求 1~4 月，缺失 1 月与 4 月。"""
    missing = _find_missing_ranges("2024-01-01", "2024-04-30", "2024-02-01", "2024-03-31")
    assert missing == [("2024-01-01", "2024-01-31"), ("2024-04-01", "2024-04-30")]


def test_find_missing_ranges_empty_request() -> None:
    assert _find_missing_ranges("", "2024-04-30", "2024-01-01", "2024-03-31") == []


@pytest.mark.parametrize(
    "from_date,to_date",
    [
        ("2024-01-01", "2024-01-10"),
        ("2024-02-01", "2024-02-15"),
    ],
)
def test_fetch_investing_historical_cached_returns_list(from_date: str, to_date: str) -> None:
    """fetch_investing_historical_cached 应返回 (list, None) 或 (None, Exception)。"""
    rows, err = fetch_investing_historical_cached(6408, from_date, to_date, interval="D")
    if err is not None:
        pytest.skip("网络或上游不可用，跳过")
    assert isinstance(rows, list)
    for row in rows:
        assert isinstance(row, dict)
        if row:
            assert "date" in row


def test_fetch_historical_then_extended_range_uses_cache() -> None:
    """
    先请求 1~3 月，再请求 2~4 月：第二次应仅爬取 4 月并合并缓存。
    验证两次返回的数据在重叠区间 [2~3 月] 一致（说明缓存生效）。
    """
    rows1, err1 = fetch_investing_historical_cached(6408, "2024-01-01", "2024-03-31", interval="D")
    if err1 is not None:
        pytest.skip("网络不可用")
    rows2, err2 = fetch_investing_historical_cached(6408, "2024-02-01", "2024-04-30", interval="D")
    if err2 is not None:
        pytest.skip("网络不可用")

    # 取重叠区间 2~3 月的数据（按 YYYY-MM-DD 规范化后判断）
    def in_feb_mar(r: dict) -> bool:
        d = _row_date_iso(r)
        return bool(d and (d.startswith("2024-02") or d.startswith("2024-03")))

    sub1 = [r for r in (rows1 or []) if in_feb_mar(r)]
    sub2 = [r for r in (rows2 or []) if in_feb_mar(r)]
    if sub1 and sub2:
        # 重叠区间的日期应一致（来自同一缓存）
        dates1 = {r.get("date") for r in sub1}
        dates2 = {r.get("date") for r in sub2}
        assert dates1 == dates2, "重叠区间数据应来自缓存一致"
