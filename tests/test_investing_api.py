# -*- coding: utf-8 -*-
"""
Investing 接口测试：覆盖所有 Investing API 及 8 类资产类型。
/api/public/{item_id} 与 /api/private/{item_id}；
列表（search）与历史（history）两种用法。
若 investiny 未安装或上游 403/网络失败，可返回 502。
"""
import pytest
from fastapi.testclient import TestClient

from aktools.core.investing import INVESTING_ITEM_IDS
from aktools.main import app

client = TestClient(app)

# 所有支持的资产类型：item_id -> 说明（与 docs/api.md 一致）
INVESTING_ASSET_TYPES = {
    "investing_index": "指数",
    "investing_stock_global": "全球股票",
    "investing_futures": "期货",
    "investing_fx": "货币",
    "investing_etf": "交易所交易基金",
    "investing_bond": "国债",
    "investing_fund": "基金",
    "investing_crypto": "虚拟货币",
}


def test_investing_supported_asset_types() -> None:
    """支持的资产类型必须与 INVESTING_ITEM_IDS 一致，且为 8 类。"""
    assert INVESTING_ITEM_IDS == set(INVESTING_ASSET_TYPES), (
        "INVESTING_ITEM_IDS 与 INVESTING_ASSET_TYPES 应一致"
    )
    assert len(INVESTING_ITEM_IDS) == 8, "应支持 8 类资产"


@pytest.mark.parametrize("item_id", sorted(INVESTING_ITEM_IDS))
def test_investing_public_list_each_asset_type(item_id: str) -> None:
    """每类资产的列表接口：GET /api/public/{item_id}?limit=2，应返回 200+列表 或 502。"""
    resp = client.get(f"/api/public/{item_id}", params={"limit": "2"})
    assert resp.status_code in (200, 502), resp.text
    if resp.status_code == 200:
        data = resp.json()
        assert isinstance(data, list), f"{item_id} 应返回 list"
        for item in data:
            assert isinstance(item, dict), f"{item_id} 每项应为 dict"


def test_investing_public_list_no_params() -> None:
    """列表接口无参数时使用默认 limit，应返回 200+列表 或 502。"""
    resp = client.get("/api/public/investing_index")
    assert resp.status_code in (200, 502), resp.text
    if resp.status_code == 200:
        assert isinstance(resp.json(), list)


def test_investing_public_list_with_query() -> None:
    """列表接口带 query 参数（如搜索 AAPL）。"""
    resp = client.get(
        "/api/public/investing_stock_global",
        params={"query": "AAPL", "limit": "3"},
    )
    assert resp.status_code in (200, 502), resp.text
    if resp.status_code == 200:
        data = resp.json()
        assert isinstance(data, list)


def test_investing_public_list_with_exchange() -> None:
    """列表接口带 exchange 参数。"""
    resp = client.get(
        "/api/public/investing_stock_global",
        params={"query": "AAPL", "limit": "2", "exchange": "NASDAQ"},
    )
    assert resp.status_code in (200, 502), resp.text
    if resp.status_code == 200:
        assert isinstance(resp.json(), list)


@pytest.mark.parametrize("item_id", sorted(INVESTING_ITEM_IDS))
def test_investing_public_historical_each_asset_type(item_id: str) -> None:
    """每类资产的历史接口：investing_id + from_date + to_date，应返回 200+列表 或 502。"""
    resp = client.get(
        f"/api/public/{item_id}",
        params={
            "investing_id": "6408",
            "from_date": "2024-01-01",
            "to_date": "2024-01-10",
        },
    )
    assert resp.status_code in (200, 502), resp.text
    if resp.status_code == 200:
        data = resp.json()
        assert isinstance(data, list), f"{item_id} 历史应返回 list"
        for row in data:
            assert isinstance(row, dict)
            if row:
                assert "date" in row or "open" in row or "close" in row, (
                    f"{item_id} 历史行应含 date/open/close 等"
                )


def test_investing_public_historical_with_interval() -> None:
    """历史接口带 interval 参数（D/W/M）。"""
    resp = client.get(
        "/api/public/investing_stock_global",
        params={
            "investing_id": "6408",
            "from_date": "2024-01-01",
            "to_date": "2024-01-31",
            "interval": "W",
        },
    )
    assert resp.status_code in (200, 502), resp.text
    if resp.status_code == 200:
        assert isinstance(resp.json(), list)


def test_investing_public_historical_invalid_id() -> None:
    """历史接口 investing_id 非数字时应返回 502 或上游错误。"""
    resp = client.get(
        "/api/public/investing_stock_global",
        params={
            "investing_id": "not_a_number",
            "from_date": "2024-01-01",
            "to_date": "2024-01-10",
        },
    )
    assert resp.status_code in (200, 502), resp.text


def test_investing_public_quotes_symbols() -> None:
    """实时行情：symbols=AAPL 应返回 200+列表（含 lp 等价格）或 502。"""
    resp = client.get(
        "/api/public/investing_stock_global",
        params={"symbols": "AAPL"},
    )
    assert resp.status_code in (200, 502), resp.text
    if resp.status_code == 200:
        data = resp.json()
        assert isinstance(data, list), "quotes 应返回 list"
        assert len(data) >= 1, "至少应有一条行情"
        row = data[0]
        assert isinstance(row, dict)
        assert row.get("symbol") == "AAPL"
        assert row.get("lp") is not None, "行情应含 lp（最近价）"
        date_str = row.get("date")
        assert isinstance(date_str, str) and " " in date_str and ":" in date_str, (
            "实时行情应包含分钟级时间（如 MM/DD/YYYY HH:MM）"
        )


def test_investing_public_quotes_multiple_symbols() -> None:
    """实时行情：多标的 symbols=AAPL,MSFT。"""
    resp = client.get(
        "/api/public/investing_stock_global",
        params={"symbols": "AAPL,MSFT"},
    )
    assert resp.status_code in (200, 502), resp.text
    if resp.status_code == 200:
        assert isinstance(resp.json(), list)


@pytest.mark.parametrize("item_id", sorted(INVESTING_ITEM_IDS))
def test_investing_private_list_requires_auth(item_id: str) -> None:
    """私人接口无 Token 时应返回 401。"""
    resp = client.get(f"/api/private/{item_id}", params={"limit": "1"})
    assert resp.status_code == 401, f"{item_id} 未认证应 401"


def test_investing_unknown_item_404() -> None:
    """既非 Investing 也非 AKShare 的 item_id 应 404。"""
    resp = client.get("/api/public/not_an_akshare_or_investing_id_xyz")
    assert resp.status_code == 404
    assert "error" in resp.json()
