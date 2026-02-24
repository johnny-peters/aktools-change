# -*- coding: utf-8 -*-
"""
Investing.com 数据抓取模块 (cn.investing.com)
支持：指数、全球股票(非A股港股)、期货、货币、ETF、国债、基金、虚拟货币
使用 investiny 访问公开数据，无需登录。
历史数据支持智能缓存：按 investing_id+interval 存储日期区间，部分命中时仅爬取缺失区间并合并。
"""
import logging
import re
from datetime import datetime, timezone, timedelta
from threading import Lock
from typing import Any, Dict, List, Optional, Tuple, Union
from uuid import uuid4

logger = logging.getLogger(name="AKToolsLog")

# 历史数据缓存：按 (investing_id, interval) 存储 {from_date, to_date, rows}，支持部分命中合并
_HISTORICAL_CACHE: Dict[Tuple[int, str], Dict[str, Any]] = {}
_HISTORICAL_CACHE_LOCK = Lock()

# 优先使用 curl_cffi 模拟浏览器 TLS/指纹，以绕过 Investing.com 的 Cloudflare 403
_USE_CURL_CFFI: Optional[bool] = None
_CURL_SESSION: Optional[Any] = None


def _get_http_client():
    import os as _os

    global _USE_CURL_CFFI, _CURL_SESSION
    if _USE_CURL_CFFI is None:
        try:
            from curl_cffi import requests as _curl_requests  # noqa: F401
            _USE_CURL_CFFI = True
            logger.info("Investing: 使用 curl_cffi 作为 HTTP 客户端（可绕过 Cloudflare 403）")
        except ImportError:
            _USE_CURL_CFFI = False
            logger.warning("Investing: curl_cffi 未安装，使用 httpx，tvc6 API 可能返回 403")
    if _USE_CURL_CFFI:
        if _CURL_SESSION is None:
            from curl_cffi import requests as curl_requests

            # 可通过环境变量 INVESTING_IMPERSONATE 覆盖（如 chrome124、safari184），以应对 403
            impersonate = _os.getenv("INVESTING_IMPERSONATE", "chrome124")
            _CURL_SESSION = curl_requests.Session(impersonate=impersonate)
            logger.info("Investing: curl_cffi impersonate=%s", impersonate)
        return ("curl_cffi", _CURL_SESSION)
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


def _normalize_yyyymmdd(s: str) -> str:
    """将日期字符串规范为 YYYY-MM-DD，用于区间比较与排序。"""
    s = (s or "").strip()
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    m = re.match(r"(\d{1,2})/(\d{1,2})/(\d{4})", s)
    if m:
        mm, dd, yyyy = m.group(1).zfill(2), m.group(2).zfill(2), m.group(3)
        return f"{yyyy}-{mm}-{dd}"
    return s


def _row_date_iso(row: Dict[str, Any]) -> str:
    """从行情行提取可比较的日期字符串 YYYY-MM-DD。"""
    d = row.get("date")
    if not d or not isinstance(d, str):
        return ""
    return _normalize_yyyymmdd(d)


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
        # impersonate 已设置 UA 等，仅补充 CORS 相关头以降低 403 概率
        resp = client.get(
            url,
            params=params,
            timeout=timeout,
            headers={
                "Referer": "https://cn.investing.com/",
                "Origin": "https://cn.investing.com",
            },
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
        # 仅保留自身类型 + Forex + Index
        allowed_types = {t, "Forex", "Index"}
        if t == "FX":
            allowed_types.add("FX")
        filtered = []
        for row in out:
            r_type = row.get("type")
            if r_type in allowed_types:
                filtered.append(row)
        return filtered, None
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


def _date_add_days(s: str, days: int) -> str:
    """YYYY-MM-DD 加减天数。"""
    s = _normalize_yyyymmdd(s)
    if not s or len(s) != 10:
        return s
    try:
        dt = datetime(int(s[:4]), int(s[5:7]), int(s[8:10]))
        return (dt + timedelta(days=days)).strftime("%Y-%m-%d")
    except (ValueError, TypeError):
        return s


def _find_missing_ranges(
    req_from: str, req_to: str, cache_from: str, cache_to: str
) -> List[Tuple[str, str]]:
    """
    计算用户请求 [req_from, req_to] 中未被缓存 [cache_from, cache_to] 覆盖的区间。
    返回 [(from1, to1), (from2, to2), ...]，按时间顺序。缺失区间与缓存不重叠。
    """
    req_from = _normalize_yyyymmdd(req_from)
    req_to = _normalize_yyyymmdd(req_to)
    cache_from = _normalize_yyyymmdd(cache_from)
    cache_to = _normalize_yyyymmdd(cache_to)
    if not req_from or not req_to:
        return []
    if not cache_from or not cache_to:
        return [(req_from, req_to)]
    missing: List[Tuple[str, str]] = []
    if req_from < cache_from:
        end = _date_add_days(cache_from, -1)
        if end >= req_from:
            missing.append((req_from, end))
    if req_to > cache_to:
        start = _date_add_days(cache_to, 1)
        if start <= req_to:
            missing.append((start, req_to))
    return missing


def _filter_rows_by_range(
    rows: List[Dict[str, Any]], from_date: str, to_date: str
) -> List[Dict[str, Any]]:
    """按日期区间过滤并排序 rows。"""
    from_iso = _normalize_yyyymmdd(from_date)
    to_iso = _normalize_yyyymmdd(to_date)
    out = []
    for row in rows:
        d = _row_date_iso(row)
        if d and from_iso <= d <= to_iso:
            out.append(row)
    out.sort(key=lambda r: (_row_date_iso(r), r.get("date", "")))
    return out


def _merge_and_dedupe_rows(
    existing: List[Dict[str, Any]], new_rows: List[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    """合并两组行情，按日期去重（新数据优先）。"""
    seen: Dict[str, Dict[str, Any]] = {}
    for row in existing + new_rows:
        d = _row_date_iso(row)
        if d:
            seen[d] = row
    merged = list(seen.values())
    merged.sort(key=lambda r: (_row_date_iso(r), r.get("date", "")))
    return merged


def fetch_investing_historical_cached(
    investing_id: int,
    from_date: str,
    to_date: str,
    interval: Union[str, int] = "D",
) -> Tuple[Optional[List[Dict[str, Any]]], Optional[Exception]]:
    """
    带智能缓存的历史数据拉取：
    - 无缓存：爬取 [from_date, to_date]，写入缓存后返回
    - 全量命中：直接返回缓存中的请求区间
    - 部分命中：仅爬取缺失区间，与缓存合并后更新缓存，返回请求区间
    """
    interval_key = str(interval).upper() if isinstance(interval, str) else str(interval)
    cache_key = (investing_id, interval_key)
    req_from = _normalize_yyyymmdd(from_date)
    req_to = _normalize_yyyymmdd(to_date)

    with _HISTORICAL_CACHE_LOCK:
        entry = _HISTORICAL_CACHE.get(cache_key)
        if entry:
            cache_from = entry.get("from_date", "")
            cache_to = entry.get("to_date", "")
            cached_rows = entry.get("rows") or []
            if cache_from and cache_to and cached_rows and req_from >= cache_from and req_to <= cache_to:
                out = _filter_rows_by_range(cached_rows, from_date, to_date)
                logger.info("Investing 历史缓存全量命中: id=%s [%s ~ %s]", investing_id, req_from, req_to)
                return out, None
            missing = _find_missing_ranges(req_from, req_to, cache_from, cache_to)
        else:
            missing = [(req_from, req_to)] if req_from and req_to else []
            cached_rows = []
            cache_from = ""
            cache_to = ""

    if not missing:
        out = _filter_rows_by_range(cached_rows, from_date, to_date)
        return out, None

    all_new_rows: List[Dict[str, Any]] = []
    for m_from, m_to in missing:
        rows, err = fetch_investing_historical(investing_id, m_from, m_to, interval=interval)
        if err is not None:
            with _HISTORICAL_CACHE_LOCK:
                entry = _HISTORICAL_CACHE.get(cache_key)
                if entry and entry.get("rows"):
                    out = _filter_rows_by_range(entry["rows"], from_date, to_date)
                    logger.info("Investing 爬取缺失区间失败，返回已有缓存: id=%s", investing_id)
                    return out, None
            return None, err
        if rows:
            all_new_rows.extend(rows)
            logger.info("Investing 爬取缺失区间: id=%s [%s ~ %s]", investing_id, m_from, m_to)

    merged = _merge_and_dedupe_rows(cached_rows, all_new_rows)
    if not merged:
        return [], None
    new_from = _row_date_iso(merged[0])
    new_to = _row_date_iso(merged[-1])
    if cache_from and cache_to:
        new_from = min(new_from, _normalize_yyyymmdd(cache_from)) if new_from else cache_from
        new_to = max(new_to, _normalize_yyyymmdd(cache_to)) if new_to else cache_to

    with _HISTORICAL_CACHE_LOCK:
        _HISTORICAL_CACHE[cache_key] = {
            "from_date": new_from,
            "to_date": new_to,
            "rows": merged,
        }

    out = _filter_rows_by_range(merged, from_date, to_date)
    return out, None


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
        content, err = fetch_investing_historical_cached(id_int, from_date, to_date, interval=interval)
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
