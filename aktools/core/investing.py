# -*- coding: utf-8 -*-
"""
Investing.com 数据抓取模块 (cn.investing.com)
支持：指数、全球股票(非A股港股)、期货、货币、ETF、国债、基金、虚拟货币
使用 investiny 访问公开数据，无需登录。
历史数据支持智能缓存：按 investing_id+interval 存储日期区间，部分命中时仅爬取缺失区间并合并。

对上证/深圳 A 股（如 600519.SH、002340.SZ）：Investing 无数据，自动回退到 AKShare 获取。
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
_CURL_IMPERSONATE: Optional[str] = None


def _get_http_client():
    import os as _os

    global _USE_CURL_CFFI, _CURL_SESSION, _CURL_IMPERSONATE
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

            # 可通过环境变量 INVESTING_IMPERSONATE 覆盖（如 chrome136、safari184），以应对 403
            impersonate = _os.getenv("INVESTING_IMPERSONATE", "chrome136")
            _CURL_SESSION = curl_requests.Session(impersonate=impersonate)
            _CURL_IMPERSONATE = impersonate
            logger.info("Investing: curl_cffi impersonate=%s", impersonate)
        return ("curl_cffi", _CURL_SESSION, _CURL_IMPERSONATE)
    import httpx
    return ("httpx", httpx, None)

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
    import os as _os

    global _CURL_SESSION, _CURL_IMPERSONATE
    url = f"https://tvc6.investing.com/{uuid4().hex}/0/0/0/0/{endpoint}"
    client_type, client, impersonate = _get_http_client()
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
        if resp.status_code == 403:
            try:
                from curl_cffi import requests as curl_requests

                candidates = ["chrome136", "safari184", "chrome124"]
                env_imp = _os.getenv("INVESTING_IMPERSONATE", "").strip()
                if env_imp:
                    candidates = [env_imp] + [x for x in candidates if x != env_imp]
                if impersonate:
                    candidates = [x for x in candidates if x != impersonate] + [impersonate]
                for imp in candidates:
                    try:
                        session = curl_requests.Session(impersonate=imp)
                        retry_resp = session.get(
                            url,
                            params=params,
                            timeout=timeout,
                            headers={
                                "Referer": "https://cn.investing.com/",
                                "Origin": "https://cn.investing.com",
                            },
                        )
                        if retry_resp.status_code == 200:
                            _CURL_SESSION = session
                            _CURL_IMPERSONATE = imp
                            resp = retry_resp
                            logger.info("Investing: 403 后切换 impersonate=%s 重试成功", imp)
                            break
                    except Exception:
                        continue
            except Exception:
                pass
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
    query_symbol = symbol.strip()
    if item_id == "investing_stock_global" and _is_a_share_symbol(query_symbol):
        # search 对 600519.SH / 002340.SZ 返回空，A 股用纯 6 位代码查询
        query_symbol = _normalize_a_share_code(query_symbol) or query_symbol
    kwargs: Dict[str, Any] = {"query": query_symbol, "limit": 5, "type": search_type}
    if exchange:
        kwargs["exchange"] = exchange
    try:
        res = _request_to_investing(endpoint="search", params=kwargs)  # type: ignore
    except Exception:
        return None
    if not isinstance(res, list) or not res:
        return None
    # A 股优先做精确匹配，避免 query=002340 命中到非 A 股 ticker。
    if item_id == "investing_stock_global" and _is_a_share_symbol(symbol):
        code = _normalize_a_share_code(symbol)
        m = re.match(r"^\d{6}(?:\.(SH|SZ))?$", symbol.strip().upper())
        suffix = m.group(1) if m else ""
        for r in res:
            if not isinstance(r, dict):
                continue
            symbol_text = str(r.get("symbol") or "").strip().upper()
            exchange_text = str(r.get("exchange") or "").strip().upper()
            if code and not (symbol_text == code or symbol_text.endswith(code)):
                continue
            if suffix == "SH" and exchange_text and ("SHANGHAI" not in exchange_text and "SSE" not in exchange_text):
                continue
            if suffix == "SZ" and exchange_text and ("SHENZHEN" not in exchange_text and "SZSE" not in exchange_text):
                continue
            ticker = r.get("ticker") or r.get("id")
            if ticker is None:
                continue
            try:
                return int(ticker)
            except (TypeError, ValueError):
                continue
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


def _is_a_share_symbol(symbol: str) -> bool:
    """判断是否为 A 股代码格式（6 位数字，可选 .SH/.SZ 后缀）。"""
    if not symbol or not isinstance(symbol, str):
        return False
    s = symbol.strip().upper()
    return bool(re.match(r"^\d{6}(\.(SH|SZ))?$", s))


def _normalize_a_share_code(symbol: str) -> str:
    """从 600519.SH 或 002340.SZ 提取纯代码 600519、002340。"""
    if not symbol or not isinstance(symbol, str):
        return ""
    s = symbol.strip().upper()
    m = re.match(r"^(\d{6})(\.(SH|SZ))?$", s)
    return m.group(1) if m else ""


def _fetch_a_share_quote_akshare(
    symbol_display: str, code: str, retries: int = 2
) -> Optional[Dict[str, Any]]:
    """通过 AKShare 获取 A 股行情，拼成与 Investing 兼容的 quote 格式。"""
    try:
        import akshare as ak  # noqa: F401
    except ImportError:
        return None
    today = datetime.now(timezone.utc)
    end_date = today.strftime("%Y%m%d")
    from_date = (today - timedelta(days=10)).strftime("%Y%m%d")
    last_err = None
    for attempt in range(max(1, retries)):
        try:
            df = ak.stock_zh_a_hist(
                symbol=code, period="daily", start_date=from_date, end_date=end_date, adjust=""
            )
            last_err = None
            break
        except Exception as e:
            last_err = e
            if attempt < retries - 1:
                import time as _time
                _time.sleep(0.5 * (attempt + 1))
    if last_err:
        logger.warning("akshare A股行情 %s failed: %s", code, last_err)
        return None
    if df is None or len(df) < 1:
        return None
    last = df.iloc[-1]
    prev = df.iloc[-2] if len(df) >= 2 else last
    try:
        close = last["收盘"]
        open_p = last["开盘"]
        high_p = last["最高"]
        low_p = last["最低"]
        volume = last["成交量"]
        date_val = last["日期"]
        prev_close = prev["收盘"] if len(df) >= 2 else open_p
    except (KeyError, TypeError):
        return None
    if close is None or (isinstance(close, float) and (close != close)):  # NaN
        return None
    date_str = date_val.strftime("%Y-%m-%d") if hasattr(date_val, "strftime") else str(date_val)
    ch: Optional[float] = None
    chp: Optional[float] = None
    if prev_close is not None and prev_close != 0:
        try:
            ch = round(float(close) - float(prev_close), 6)
            chp = round((ch / float(prev_close)) * 100, 4)
        except (TypeError, ValueError):
            pass
    return {
        "symbol": symbol_display,
        "lp": close,
        "open_price": open_p,
        "high_price": high_p,
        "low_price": low_p,
        "prev_close_price": prev_close,
        "ch": ch,
        "chp": chp,
        "volume": volume,
        "date": date_str,
    }


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


def _quote_from_history_with_fallback(
    symbol_name: str,
    investing_id: int,
    from_date: str,
    to_date: str,
    intervals: List[Union[str, int]],
) -> Optional[Dict[str, Any]]:
    """按多个分辨率依次尝试生成 quote，直到拿到有效价格。"""
    for iv in intervals:
        row = _quote_from_history(symbol_name, investing_id, from_date, to_date, interval=iv)
        if row is not None and _quote_has_price(row):
            return row
    return None


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
            is_a_share = item_id == "investing_stock_global" and _is_a_share_symbol(sym)
            tid = _resolve_symbol_to_investing_id(item_id, sym, exchange=exchange)
            if tid is not None:
                is_a_share = item_id == "investing_stock_global" and _is_a_share_symbol(sym)
                # A 股先尝试 1 分钟，若 no_data 再尝试 5 分钟和日线（仍走 Investing）
                intervals: List[Union[str, int]] = [1, 5, "D"] if is_a_share else [1]
                row = _quote_from_history_with_fallback(sym, tid, from_date, to_date, intervals)
                if row is not None:
                    out.append(row)
                    if is_a_share:
                        logger.info("investing quotes: A股 %s 通过 Investing 获取", sym)
                    continue
                if is_a_share:
                    logger.warning("investing quotes: A股 %s Investing 无数据，回退 AKShare", sym)
            elif not is_a_share:
                logger.warning("investing quotes: no id for symbol=%s", sym)

            if is_a_share:
                code = _normalize_a_share_code(sym)
                if code:
                    row = _fetch_a_share_quote_akshare(sym, code)
                    if row is not None:
                        out.append(row)
                        logger.info("investing quotes: A股 %s 通过 AKShare 回退成功", sym)
                    else:
                        logger.warning("investing quotes: A股 %s AKShare 无数据", sym)
                else:
                    logger.warning("investing quotes: invalid A-share symbol=%s", sym)
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


def _fetch_a_share_historical_akshare(
    code: str, from_date: str, to_date: str, interval: str = "D", retries: int = 2
) -> Tuple[Optional[List[Dict[str, Any]]], Optional[Exception]]:
    """通过 AKShare 获取 A 股历史 K 线，转为 Investing 兼容格式。"""
    try:
        import akshare as ak  # noqa: F401
    except ImportError:
        return None, Exception("akshare 未安装，无法获取 A 股历史数据")
    from_fmt = from_date.replace("-", "")[:8]
    to_fmt = to_date.replace("-", "")[:8]
    period = "daily" if interval in ("D", "d", "1") else ("weekly" if interval in ("W", "w") else "monthly")
    last_err = None
    for attempt in range(max(1, retries)):
        try:
            df = ak.stock_zh_a_hist(
                symbol=code, period=period, start_date=from_fmt, end_date=to_fmt, adjust=""
            )
            last_err = None
            break
        except Exception as e:
            last_err = e
            if attempt < retries - 1:
                import time as _time
                _time.sleep(0.5 * (attempt + 1))
    if last_err:
        return None, last_err
    if df is None or len(df) == 0:
        return [], None
    rows = []
    for _, r in df.iterrows():
        d = r.get("日期")
        date_str = d.strftime("%m/%d/%Y") if hasattr(d, "strftime") else str(d)
        rows.append({
            "date": date_str,
            "open": r.get("开盘"),
            "high": r.get("最高"),
            "low": r.get("最低"),
            "close": r.get("收盘"),
            "volume": r.get("成交量"),
        })
    return rows, None


def fetch_investing_data(
    item_id: str,
    params: Dict[str, str],
) -> Tuple[Optional[Any], Optional[Exception]]:
    """
    统一入口：根据 params 决定拉取列表、历史或实时行情。
    - 若提供 symbols（如 symbols=AAPL 或 symbols=600519.SH）：拉取实时行情（quotes）。
    - 若提供 investing_id + from_date + to_date：拉取历史数据。
    - 若提供 symbol + from_date + to_date 且为 A 股：先用 Investing，失败再回退 AKShare。
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

    # A 股历史：symbol + from_date + to_date（无 investing_id 时）
    symbol_param = (params.get("symbol") or "").strip()
    if not pid and symbol_param and from_date and to_date and item_id == "investing_stock_global":
        if _is_a_share_symbol(symbol_param):
            code = _normalize_a_share_code(symbol_param)
            if code:
                interval = (params.get("interval") or "D").strip()
                try:
                    interval = int(interval)
                except ValueError:
                    interval = interval.upper()
                tid = _resolve_symbol_to_investing_id(item_id, symbol_param, exchange=(params.get("exchange") or "").strip())
                if tid is not None:
                    content, err = fetch_investing_historical_cached(tid, from_date, to_date, interval=interval)
                    if err is None and content:
                        logger.info("investing 历史: A股 %s 通过 Investing 获取", symbol_param)
                        return content or [], None
                    logger.warning("investing 历史: A股 %s Investing 无数据，回退 AKShare", symbol_param)
                content, err = _fetch_a_share_historical_akshare(code, from_date, to_date, str(interval))
                if err is not None:
                    return None, err
                logger.info("investing 历史: A股 %s 通过 AKShare 获取", symbol_param)
                return content or [], None

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
