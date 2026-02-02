# -*- coding: utf-8 -*-
"""
Investing.com 数据抓取模块 (cn.investing.com)
支持：指数、全球股票(非A股港股)、期货、货币、ETF、国债、基金、虚拟货币
使用 investiny 访问公开数据，无需登录。
"""
import logging
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional, Tuple, Union
from uuid import uuid4

logger = logging.getLogger(name="AKToolsLog")

# 优先使用 curl_cffi 模拟浏览器 TLS/指纹，以绕过 Investing.com 的 Cloudflare 403
_USE_CURL_CFFI: Optional[bool] = None


def _get_http_client():
    global _USE_CURL_CFFI
    if _USE_CURL_CFFI is None:
        try:
            from curl_cffi import requests as _curl_requests  # noqa: F401
            _USE_CURL_CFFI = True
        except ImportError:
            _USE_CURL_CFFI = False
    if _USE_CURL_CFFI:
        from curl_cffi import requests as curl_requests
        return ("curl_cffi", curl_requests)
    import httpx
    return ("httpx", httpx)

# item_id -> investiny search type
INVESTING_ITEM_IDS = {
    "investing_index",
    "investing_stock_global",
    "investing_futures",
    "investing_fx",
    "investing_etf",
    "investing_bond",
    "investing_fund",
    "investing_crypto",
}

_INVESTING_TYPE_MAP: Dict[str, str] = {
    "investing_index": "Index",
    "investing_stock_global": "Stock",
    "investing_futures": "Future",
    "investing_fx": "FX",
    "investing_etf": "ETF",
    "investing_bond": "Yield",
    "investing_fund": "Fund",
    "investing_crypto": "Crypto",
}

# 默认交易所/查询词，用于无参数时返回列表
_DEFAULT_QUERY_MAP: Dict[str, str] = {
    "investing_index": "",
    "investing_stock_global": "",
    "investing_futures": "",
    "investing_fx": "",
    "investing_etf": "",
    "investing_bond": "",
    "investing_fund": "",
    "investing_crypto": "BTC",  # tvc6 search 未支持 type=Crypto，用 query 拉取加密货币
    "investing_fund": "fund",   # Fund 未在 investiny type 中，用 query 拉取
}

# tvc6 search 不支持的 type 时传空，靠 query 拉取（investiny 仅支持 Stock/ETF/Commodity/Index/Future/Yield/FX）
_SEARCH_TYPE_OVERRIDE: Dict[str, str] = {
    "investing_crypto": "",  # 不传 type，用 query=BTC 等获取加密货币
    "investing_fund": "",    # Fund 未在 investiny 类型中，用空 + query=fund
}


def _ensure_investiny() -> Tuple[bool, Optional[str]]:
    try:
        import investiny  # noqa: F401
        return True, None
    except ImportError as e:
        return False, f"investiny 未安装: {e}"


def _date_to_investing(s: str) -> str:
    """将 YYYYMMDD 或 YYYY-MM-DD 转为 DD/MM/YYYY（用于部分接口）。"""
    s = (s or "").strip().replace("-", "")
    if len(s) == 8:
        return f"{s[6:8]}/{s[4:6]}/{s[0:4]}"
    return s


def _date_to_investiny(s: str) -> str:
    """将 YYYY-MM-DD 转为 MM/DD/YYYY，供 investiny calculate_date_intervals 使用。"""
    s = (s or "").strip().replace("-", "")
    if len(s) == 8:
        return f"{s[4:6]}/{s[6:8]}/{s[0:4]}"
    return s


def _date_to_investiny_time(s: str, hm: str = "00:00") -> str:
    """将 YYYY-MM-DD 转为 MM/DD/YYYY HH:MM，供分钟级 interval 使用。"""
    date_part = _date_to_investiny(s)
    return f"{date_part} {hm}".strip()


def _build_headers() -> Dict[str, str]:
    return {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Accept-Encoding": "gzip, deflate, br",
        "Referer": "https://cn.investing.com/",
        "Origin": "https://cn.investing.com",
        "DNT": "1",
        "Connection": "keep-alive",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-site",
    }


def _request_to_investing(
    endpoint: str, params: Dict[str, Any], timeout: int = 12
) -> Union[Dict[str, Any], List[Dict[str, Any]]]:
    url = f"https://tvc6.investing.com/{uuid4().hex}/0/0/0/0/{endpoint}"
    client_type, client = _get_http_client()
    if client_type == "curl_cffi":
        # 模拟 Chrome 浏览器 TLS/指纹，提高通过 Cloudflare 的概率
        resp = client.get(
            url,
            params=params,
            timeout=timeout,
            impersonate="chrome120",
            referer="https://cn.investing.com/",
        )
    else:
        headers = _build_headers()
        resp = client.get(url, params=params, headers=headers, timeout=timeout)
    if resp.status_code != 200:
        raise ConnectionError(
            f"Request to Investing.com API failed with error code: {resp.status_code}."
        )
    data = resp.json()
    if endpoint in ["history", "quotes"] and data.get("s") not in (None, "ok"):
        if "nextTime" in data:
            raise ConnectionError(
                f"Request to Investing.com API failed with error message: {data.get('s')}, "
                f"try `from_date={datetime.fromtimestamp(data['nextTime'], tz=timezone.utc).strftime('%m/%d/%Y')}`."
            )
        raise ConnectionError(
            f"Request to Investing.com API failed with error message: {data.get('s')}."
        )
    return data


def _investing_info(investing_id: int) -> Dict[str, Any]:
    return _request_to_investing(endpoint="symbols", params={"symbol": investing_id})  # type: ignore


def _calculate_date_intervals(from_date: str, to_date: str, interval: Union[str, int]) -> Tuple[List[datetime], List[datetime]]:
    from investiny.utils import calculate_date_intervals  # noqa: WPS433
    from investiny.config import Config  # noqa: WPS433

    if not from_date:
        return calculate_date_intervals(from_date=None, to_date=None, interval=interval)  # type: ignore
    return calculate_date_intervals(
        from_date=from_date or None,
        to_date=to_date or None,
        interval=interval,  # type: ignore
    )


def _format_datetime(dt: datetime, interval: Union[str, int]) -> str:
    from investiny.config import Config  # noqa: WPS433
    fmt = Config.time_format if interval not in ["D", "W", "M"] else Config.date_format
    return dt.strftime(fmt)


def fetch_investing_list(
    item_id: str,
    query: str = "",
    limit: int = 50,
    exchange: str = "",
) -> Tuple[Optional[List[Dict[str, Any]]], Optional[Exception]]:
    """拉取 Investing 某类型的资产列表（search_assets）。"""
    ok, err = _ensure_investiny()
    if not ok:
        return None, Exception(err)
    t = _INVESTING_TYPE_MAP.get(item_id)
    if not t:
        return None, ValueError(f"unknown item_id: {item_id}")
    try:
        # 部分类型 tvc6 不认，用空 type + 默认 query 拉取（如加密货币、基金）
        search_type = _SEARCH_TYPE_OVERRIDE.get(item_id, t)
        q = query or _DEFAULT_QUERY_MAP.get(item_id) or ("" if search_type == "" else t)
        kwargs = {"query": q, "limit": limit, "type": search_type}
        if exchange:
            kwargs["exchange"] = exchange
        res = _request_to_investing(endpoint="search", params=kwargs)  # type: ignore
        if res is None:
            return [], None
        out = []
        for r in res:
            if isinstance(r, dict):
                out.append({k: v for k, v in r.items()})
            else:
                out.append(dict(r))
        return out, None
    except Exception as e:
        logger.exception("investing list %s failed: %s", item_id, e)
        return None, e


def _resolve_symbol_to_investing_id(
    item_id: str, symbol: str, exchange: str = ""
) -> Optional[int]:
    """通过 search 将资产名称/代码解析为 investing_id（ticker）。"""
    t = _INVESTING_TYPE_MAP.get(item_id)
    if not t:
        return None
    search_type = _SEARCH_TYPE_OVERRIDE.get(item_id, t)
    kwargs: Dict[str, Any] = {"query": symbol.strip(), "limit": 5, "type": search_type}
    if exchange:
        kwargs["exchange"] = exchange
    try:
        res = _request_to_investing(endpoint="search", params=kwargs)  # type: ignore
    except Exception:
        return None
    if not isinstance(res, list) or not res:
        return None
    for r in res:
        if not isinstance(r, dict):
            continue
        ticker = r.get("ticker") or r.get("id")
        if ticker is None:
            continue
        try:
            return int(ticker)
        except (TypeError, ValueError):
            continue
    return None


def _quote_has_price(row: Dict[str, Any]) -> bool:
    """判断一条行情是否已有有效价格（lp 或 close 等）。"""
    lp = row.get("lp")
    if lp is not None and (isinstance(lp, (int, float)) or (isinstance(lp, str) and lp.strip())):
        return True
    close = row.get("close")
    return close is not None and (isinstance(close, (int, float)) or (isinstance(close, str) and close.strip()))


def _quote_from_history(
    symbol_name: str,
    investing_id: int,
    from_date: str,
    to_date: str,
    interval: Union[str, int] = "D",
) -> Optional[Dict[str, Any]]:
    """用最近一根 K 线拼成一条「近似实时」行情（lp=close, ch/chp 等）。"""
    rows, err = fetch_investing_historical(investing_id, from_date, to_date, interval=interval)
    if err or not isinstance(rows, list) or len(rows) < 1:
        return None
    last = rows[-1]
    prev = rows[-2] if len(rows) >= 2 else None
    close = last.get("close")
    if close is None:
        return None
    open_p = last.get("open")
    high_p = last.get("high")
    low_p = last.get("low")
    prev_close = prev.get("close") if prev else open_p
    ch: Optional[float] = None
    chp: Optional[float] = None
    if prev_close is not None and isinstance(prev_close, (int, float)) and isinstance(close, (int, float)):
        try:
            ch = round(float(close) - float(prev_close), 6)
            chp = round((ch / float(prev_close)) * 100, 4) if prev_close != 0 else None
        except (TypeError, ValueError):
            pass
    return {
        "symbol": symbol_name,
        "lp": close,
        "open_price": open_p,
        "high_price": high_p,
        "low_price": low_p,
        "prev_close_price": prev_close,
        "ch": ch,
        "chp": chp,
        "volume": last.get("volume"),
        "date": last.get("date"),
    }


def fetch_investing_quotes(
    item_id: str,
    symbols: List[str],
    exchange: str = "",
) -> Tuple[Optional[List[Dict[str, Any]]], Optional[Exception]]:
    """
    拉取 Investing 近似实时行情。tvc6 quotes 用名称请求常返回 lp/ch 为 null，
    故统一用「search 解析 symbol -> investing_id + 最近一分钟 K 线」拼 lp/ch/chp。
    """
    ok, err = _ensure_investiny()
    if not ok:
        return None, Exception(err)
    symbols = [s.strip() for s in symbols if (s and s.strip())]
    if not symbols:
        return [], None
    try:
        today = datetime.now(timezone.utc)
        to_date = today.strftime("%Y-%m-%d")
        from_date = (today - timedelta(days=2)).strftime("%Y-%m-%d")
        out: List[Dict[str, Any]] = []
        for sym in symbols:
            tid = _resolve_symbol_to_investing_id(item_id, sym, exchange=exchange)
            if tid is None:
                logger.warning("investing quotes: no id for symbol=%s", sym)
                continue
            row = _quote_from_history(sym, tid, from_date, to_date, interval=1)
            if row is not None:
                out.append(row)
        return out, None
    except Exception as e:
        logger.exception("investing quotes symbols=%s failed: %s", symbols, e)
        return None, e


def fetch_investing_historical(
    investing_id: int,
    from_date: str,
    to_date: str,
    interval: Union[str, int] = "D",
) -> Tuple[Optional[List[Dict[str, Any]]], Optional[Exception]]:
    """拉取 Investing 某资产的历史数据。"""
    ok, err = _ensure_investiny()
    if not ok:
        return None, Exception(err)
    if interval in ["D", "W", "M"]:
        from_fmt = _date_to_investiny(from_date)
        to_fmt = _date_to_investiny(to_date)
    else:
        from_fmt = _date_to_investiny_time(from_date, "00:00")
        to_fmt = _date_to_investiny_time(to_date, "23:59")
    try:
        info = _investing_info(investing_id)
        has_volume = not info.get("has_no_volume", False)
        days_shift = 1 if info.get("type") == "Yield" else 0

        from_datetimes, to_datetimes = _calculate_date_intervals(
            from_date=from_fmt, to_date=to_fmt, interval=interval
        )
        rows: List[Dict[str, Any]] = []
        for to_dt, from_dt in zip(to_datetimes, from_datetimes):
            params = {
                "symbol": investing_id,
                "from": int(from_dt.timestamp()),
                "to": int(to_dt.timestamp()),
                "resolution": interval,
            }
            data = _request_to_investing(endpoint="history", params=params)  # type: ignore
            times = data.get("t", [])
            opens = data.get("o", [])
            highs = data.get("h", [])
            lows = data.get("l", [])
            closes = data.get("c", [])
            volumes = data.get("v", []) if has_volume else []
            for idx, ts in enumerate(times):
                dt = datetime.fromtimestamp(ts) - timedelta(days=days_shift)
                row = {
                    "date": _format_datetime(dt, interval),
                    "open": opens[idx] if idx < len(opens) else None,
                    "high": highs[idx] if idx < len(highs) else None,
                    "low": lows[idx] if idx < len(lows) else None,
                    "close": closes[idx] if idx < len(closes) else None,
                }
                if has_volume:
                    row["volume"] = volumes[idx] if idx < len(volumes) else None
                rows.append(row)
        return rows, None
    except Exception as e:
        logger.exception("investing historical id=%s failed: %s", investing_id, e)
        return None, e


def fetch_investing_data(
    item_id: str,
    params: Dict[str, str],
) -> Tuple[Optional[Any], Optional[Exception]]:
    """
    统一入口：根据 params 决定拉取列表、历史或实时行情。
    - 若提供 symbols（如 symbols=AAPL 或 symbols=AAPL,MSFT）：拉取实时行情（quotes）。
    - 若提供 investing_id + from_date + to_date：拉取历史数据。
    - 否则：拉取该类型资产列表（可带 query, limit, exchange）。
    返回 (list[dict], None) 或 (None, Exception)。
    """
    if item_id not in INVESTING_ITEM_IDS:
        return None, ValueError(f"unknown investing item_id: {item_id}")

    symbols_param = (params.get("symbols") or params.get("Symbols") or "").strip()
    if symbols_param:
        symbol_list = [s.strip() for s in symbols_param.split(",") if s.strip()]
        if symbol_list:
            exchange = (params.get("exchange") or "").strip()
            content, err = fetch_investing_quotes(item_id, symbol_list, exchange=exchange)
            if err is not None:
                return None, err
            return content or [], None

    pid = (params.get("investing_id") or "").strip()
    from_date = (params.get("from_date") or "").strip()
    to_date = (params.get("to_date") or "").strip()

    if pid and from_date and to_date:
        try:
            id_int = int(pid)
        except ValueError:
            return None, ValueError("investing_id 必须为数字")
        interval = (params.get("interval") or "D").strip()
        try:
            interval = int(interval)
        except ValueError:
            interval = interval.upper()
        content, err = fetch_investing_historical(id_int, from_date, to_date, interval=interval)
        if err is not None:
            return None, err
        return content or [], None

    query = (params.get("query") or "").strip()
    try:
        limit = int((params.get("limit") or "50").strip())
    except ValueError:
        limit = 50
    limit = max(1, min(limit, 200))
    exchange = (params.get("exchange") or "").strip()
    content, err = fetch_investing_list(item_id, query=query, limit=limit, exchange=exchange)
    if err is not None:
        return None, err
    return content or [], None


def is_investing_item(item_id: str) -> bool:
    return item_id in INVESTING_ITEM_IDS
