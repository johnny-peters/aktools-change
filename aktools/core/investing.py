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
import csv
import io
from datetime import datetime, timezone, timedelta
from threading import Lock
from typing import Any, Dict, List, Optional, Tuple, Union
from urllib.parse import quote_plus
from uuid import uuid4

logger = logging.getLogger(name="AKToolsLog")

# 历史数据缓存：按 (investing_id, interval) 存储 {from_date, to_date, rows}，支持部分命中合并
_HISTORICAL_CACHE: Dict[Tuple[int, str], Dict[str, Any]] = {}
_HISTORICAL_CACHE_LOCK = Lock()

# 优先使用 curl_cffi 模拟浏览器 TLS/指纹，以绕过 Investing.com 的 Cloudflare 403
_USE_CURL_CFFI: Optional[bool] = None
_CURL_SESSION: Optional[Any] = None
_CURL_IMPERSONATE: Optional[str] = None


def _env_float(name: str, default: float, min_v: float, max_v: float) -> float:
    import os as _os

    raw = _os.getenv(name, str(default)).strip()
    try:
        value = float(raw)
    except (TypeError, ValueError):
        value = default
    return max(min_v, min(value, max_v))


def _env_int(name: str, default: int, min_v: int, max_v: int) -> int:
    import os as _os

    raw = _os.getenv(name, str(default)).strip()
    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = default
    return max(min_v, min(value, max_v))


def _env_bool(name: str, default: bool = False) -> bool:
    import os as _os

    raw = _os.getenv(name, "1" if default else "0").strip().lower()
    return raw in {"1", "true", "yes", "on"}


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
    endpoint: str, params: Dict[str, Any], timeout: float = 6
) -> Union[Dict[str, Any], List[Dict[str, Any]]]:
    import os as _os

    global _CURL_SESSION, _CURL_IMPERSONATE
    timeout = _env_float("INVESTING_REQUEST_TIMEOUT", timeout, 2.0, 15.0)
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
                    candidates = [x for x in candidates if x != impersonate]
                max_retries = _env_int("INVESTING_403_MAX_RETRIES", 1, 0, 2)
                retry_timeout = _env_float("INVESTING_403_RETRY_TIMEOUT", 2.5, 1.0, 8.0)
                candidates = candidates[:max_retries]
                for imp in candidates:
                    try:
                        session = curl_requests.Session(impersonate=imp)
                        retry_resp = session.get(
                            url,
                            params=params,
                            timeout=min(timeout, retry_timeout),
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
        fallback = _fallback_search_assets(item_id, query=query, limit=limit, exchange=exchange)
        if fallback:
            logger.warning("investing list %s fallback to local catalog due to error: %s", item_id, e)
            return fallback, None
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
    is_a = item_id == "investing_stock_global" and _is_a_share_symbol(query_symbol)
    is_hk = item_id == "investing_stock_global" and _is_hk_stock_symbol(query_symbol)
    if is_a:
        # search 对 600519.SH / 002340.SZ 返回空，A 股用纯 6 位代码查询
        query_symbol = _normalize_a_share_code(query_symbol) or query_symbol
    elif is_hk:
        # search 对 0700.HK / 1810.HK 返回空，港股用纯数字代码查询
        query_symbol = _normalize_hk_code(query_symbol) or query_symbol
    kwargs: Dict[str, Any] = {"query": query_symbol, "limit": 5, "type": search_type}
    if exchange:
        kwargs["exchange"] = exchange
    try:
        res = _request_to_investing(endpoint="search", params=kwargs)  # type: ignore
    except Exception:
        return None
    if not isinstance(res, list) or not res:
        return None
    # 中国股票优先做精确匹配，避免同代码命中东京等其他市场。
    if item_id == "investing_stock_global" and (is_a or is_hk):
        code = _normalize_a_share_code(symbol) if is_a else _normalize_hk_code(symbol)
        m = re.match(r"^\d{6}(?:\.(SH|SZ))?$", symbol.strip().upper())
        suffix = m.group(1) if m else ""
        for r in res:
            if not isinstance(r, dict):
                continue
            symbol_text = str(r.get("symbol") or "").strip().upper()
            exchange_text = str(r.get("exchange") or "").strip().upper()
            if code and not (symbol_text == code or symbol_text.endswith(code)):
                continue
            if is_hk:
                if "HONG KONG" not in exchange_text and "HK" not in exchange_text:
                    continue
            else:
                if suffix == "SH" and exchange_text and ("SHANGHAI" not in exchange_text and "SSE" not in exchange_text):
                    continue
                if suffix == "SZ" and exchange_text and ("SHENZHEN" not in exchange_text and "SZSE" not in exchange_text):
                    continue
                if suffix not in {"SH", "SZ"} and exchange_text and all(
                    x not in exchange_text for x in ["SHANGHAI", "SHENZHEN", "SSE", "SZSE"]
                ):
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


def _is_hk_stock_symbol(symbol: str) -> bool:
    """判断是否为港股代码格式（4-5 位数字，可选 .HK 后缀）。"""
    if not symbol or not isinstance(symbol, str):
        return False
    s = symbol.strip().upper()
    return bool(re.match(r"^\d{4,5}(\.HK)?$", s))


def _normalize_hk_code(symbol: str) -> str:
    """从 0700.HK/1810.HK 提取纯代码，不足 4 位左补零。"""
    if not symbol or not isinstance(symbol, str):
        return ""
    s = symbol.strip().upper()
    m = re.match(r"^(\d{1,5})(?:\.HK)?$", s)
    if not m:
        return ""
    code = m.group(1)
    return code.zfill(4) if len(code) < 4 else code


def _is_china_stock_symbol(symbol: str) -> bool:
    """判断是否为中国股票代码（A 股或港股）。"""
    return _is_a_share_symbol(symbol) or _is_hk_stock_symbol(symbol)


_TENCENT_INDEX_SYMBOL_MAP: Dict[str, str] = {
    "IXIC": "usIXIC",            # 纳斯达克综合
    ".IXIC": "usIXIC",
    "NASDAQ": "usIXIC",
    "NDX": "usNDX",              # 纳斯达克100
    ".NDX": "usNDX",
    "NASDAQ100": "usNDX",
    "DJI": "usDJI",              # 道指
    ".DJI": "usDJI",
    "DOWJONES": "usDJI",
    "SPX": "usINX",              # 标普500
    ".INX": "usINX",
    "S&P500": "usINX",
    "SSEC": "sh000001",          # 上证指数
    "SSE": "sh000001",
    "SH000001": "sh000001",
    "000001.SH": "sh000001",
    "000001.SS": "sh000001",
    "上证指数": "sh000001",
}

_US_STOCK_TYPO_MAP: Dict[str, str] = {
    # 常见误拼：苹果应为 AAPL
    "APPL": "AAPL",
}

_FALLBACK_LIST_CATALOG: Dict[str, List[Dict[str, str]]] = {
    "investing_stock_global": [
        {"symbol": "AAPL", "name": "Apple Inc.", "exchange": "NASDAQ", "type": "Stock"},
        {"symbol": "NVDA", "name": "NVIDIA Corporation", "exchange": "NASDAQ", "type": "Stock"},
        {"symbol": "MSFT", "name": "Microsoft Corporation", "exchange": "NASDAQ", "type": "Stock"},
        {"symbol": "AMZN", "name": "Amazon.com Inc.", "exchange": "NASDAQ", "type": "Stock"},
        {"symbol": "TSLA", "name": "Tesla Inc.", "exchange": "NASDAQ", "type": "Stock"},
        {"symbol": "GOOGL", "name": "Alphabet Inc.", "exchange": "NASDAQ", "type": "Stock"},
    ],
    "investing_index": [
        {"symbol": "IXIC", "name": "NASDAQ Composite", "exchange": "NASDAQ", "type": "Index"},
        {"symbol": "SPX", "name": "S&P 500", "exchange": "NYSE", "type": "Index"},
        {"symbol": "DJI", "name": "Dow Jones Industrial Average", "exchange": "NYSE", "type": "Index"},
        {"symbol": "000001.SH", "name": "Shanghai Composite", "exchange": "SSE", "type": "Index"},
    ],
    "investing_crypto": [
        {"symbol": "BTC", "name": "Bitcoin", "exchange": "Fallback", "type": "Crypto"},
        {"symbol": "ETH", "name": "Ethereum", "exchange": "Fallback", "type": "Crypto"},
        {"symbol": "BNB", "name": "BNB", "exchange": "Fallback", "type": "Crypto"},
        {"symbol": "SOL", "name": "Solana", "exchange": "Fallback", "type": "Crypto"},
    ],
}


def _quote_updated_at() -> str:
    """返回统一的行情更新时间（北京时间）。"""
    cst = timezone(timedelta(hours=8))
    return datetime.now(cst).strftime("%Y-%m-%d %H:%M:%S")


def _fallback_search_assets(
    item_id: str, query: str = "", limit: int = 50, exchange: str = ""
) -> List[Dict[str, Any]]:
    """Investing search 被 403 时的候选列表降级。"""
    pool = _FALLBACK_LIST_CATALOG.get(item_id, [])
    q = (query or "").strip().upper()
    ex = (exchange or "").strip().upper()
    out: List[Dict[str, Any]] = []
    for row in pool:
        symbol = str(row.get("symbol") or "").upper()
        name = str(row.get("name") or "").upper()
        row_ex = str(row.get("exchange") or "").upper()
        if q and q not in symbol and q not in name:
            continue
        if ex and ex not in row_ex:
            continue
        out.append(dict(row))
        if len(out) >= limit:
            break
    return out


def _safe_float(v: Any) -> Optional[float]:
    try:
        s = str(v).strip()
        if not s:
            return None
        return float(s)
    except (TypeError, ValueError):
        return None


def _normalize_crypto_pair(symbol: str) -> str:
    """
    规范化加密货币交易对为 Binance 格式。
    - BTC -> BTCUSDT
    - BTC/USDT -> BTCUSDT
    - ETH-USDT -> ETHUSDT
    """
    if not symbol or not isinstance(symbol, str):
        return ""
    s = symbol.strip().upper().replace("-", "").replace("_", "")
    if "/" in s:
        base, quote = s.split("/", 1)
        s = f"{base}{quote}"
    if s.endswith(("USDT", "USD", "BUSD", "USDC", "BTC", "ETH")) and len(s) >= 6:
        return s
    # 仅输入币种时默认对 USDT
    if re.match(r"^[A-Z0-9]{2,12}$", s):
        return f"{s}USDT"
    return ""


def _normalize_tencent_symbol(item_id: str, symbol: str) -> str:
    """
    将外部 symbol 规范为腾讯行情代码。
    示例：NVDA -> usNVDA, 600519.SH -> sh600519, 000001.SH -> sh000001。
    """
    if not symbol or not isinstance(symbol, str):
        return ""
    raw = symbol.strip()
    s = raw.upper()

    # 已是腾讯代码前缀
    if re.match(r"^(US|SH|SZ|HK)[A-Z0-9\.]+$", s):
        return s.lower() if s.startswith("HK") else s[0:2].lower() + s[2:]

    if item_id == "investing_index":
        if s in _TENCENT_INDEX_SYMBOL_MAP:
            return _TENCENT_INDEX_SYMBOL_MAP[s]

    # 中国股票/指数：000001.SH, 600519, 002594.SZ
    m = re.match(r"^(\d{6})(?:\.(SH|SZ|SS))?$", s)
    if m:
        code = m.group(1)
        suffix = m.group(2) or ""
        if suffix in {"SH", "SS"}:
            return f"sh{code}"
        if suffix == "SZ":
            return f"sz{code}"
        if item_id == "investing_index":
            # 指数场景下纯数字代码优先按指数规则推断：
            # 399xxx 通常为深证指数，其余常见 000xxx/9xxxxx 归上证体系。
            if code.startswith("399"):
                return f"sz{code}"
            return f"sh{code}"
        return f"sh{code}" if code.startswith("6") else f"sz{code}"

    # 港股：0700.HK / 700.HK
    mhk = re.match(r"^(\d{1,5})(?:\.HK)?$", s)
    if mhk:
        return f"hk{mhk.group(1).zfill(5)}"

    # 美股：AAPL / NVDA / BRK.B
    if re.match(r"^[A-Z][A-Z0-9\.-]{0,9}$", s):
        us_sym = s.split(".")[0]
        us_sym = _US_STOCK_TYPO_MAP.get(us_sym, us_sym)
        return f"us{us_sym}"

    return ""


def _parse_tencent_quote(symbol_display: str, text: str) -> Optional[Dict[str, Any]]:
    """
    解析腾讯行情响应：
    v_usAAPL="...~最新价~昨收~今开~...~时间~涨跌额~涨跌幅~最高~最低~..."
    """
    if not text or "=" not in text:
        return None
    body = text.split("=", 1)[1].strip().strip(";").strip().strip('"')
    if not body:
        return None
    fields = body.split("~")
    if len(fields) < 35:
        return None

    last = _safe_float(fields[3])
    prev_close = _safe_float(fields[4])
    open_p = _safe_float(fields[5])
    high_p = _safe_float(fields[33]) if len(fields) > 33 else None
    low_p = _safe_float(fields[34]) if len(fields) > 34 else None
    ch = _safe_float(fields[31]) if len(fields) > 31 else None
    chp = _safe_float(fields[32]) if len(fields) > 32 else None
    volume = _safe_float(fields[6]) if len(fields) > 6 else None
    date_raw = fields[30] if len(fields) > 30 else ""

    # A 股常见时间格式 20260403161415，转成可读格式；美股已是 YYYY-MM-DD HH:MM:SS
    date_str = date_raw
    if re.match(r"^\d{14}$", date_raw):
        date_str = (
            f"{date_raw[0:4]}-{date_raw[4:6]}-{date_raw[6:8]} "
            f"{date_raw[8:10]}:{date_raw[10:12]}:{date_raw[12:14]}"
        )

    if last is None:
        return None
    return {
        "symbol": symbol_display,
        "lp": last,
        "open_price": open_p,
        "high_price": high_p,
        "low_price": low_p,
        "prev_close_price": prev_close,
        "ch": ch,
        "chp": chp,
        "volume": volume,
        "date": date_str,
        "updated_at": _quote_updated_at(),
    }


def _fetch_quote_tencent(symbol_display: str, item_id: str) -> Optional[Dict[str, Any]]:
    """腾讯行情兜底：用于 Investing symbol 解析失败或上游 403 时。"""
    code = _normalize_tencent_symbol(item_id, symbol_display)
    if not code:
        return None
    url = f"https://qt.gtimg.cn/q={quote_plus(code)}"
    try:
        client_type, client, _ = _get_http_client()
        if client_type == "curl_cffi":
            resp = client.get(url, timeout=10)
        else:
            resp = client.get(url, timeout=10)
        if resp.status_code != 200:
            return None
        return _parse_tencent_quote(symbol_display, resp.text)
    except Exception:
        return None


def _fetch_quote_crypto_binance(symbol_display: str) -> Optional[Dict[str, Any]]:
    """加密货币实时行情兜底（Binance 24hr ticker）。"""
    import os as _os

    # 国内网络场景默认停用 Binance，避免不可达导致串行超时。
    if not _env_bool("INVESTING_CRYPTO_USE_BINANCE", default=False):
        return None

    pair = _normalize_crypto_pair(symbol_display)
    if not pair:
        return None
    # 默认仅请求 binance.com，避免在受限网络里双端点串行超时导致首包过慢
    urls = [f"https://api.binance.com/api/v3/ticker/24hr?symbol={quote_plus(pair)}"]
    try:
        timeout = float(_os.getenv("INVESTING_CRYPTO_BINANCE_TIMEOUT", "3"))
    except ValueError:
        timeout = 3.0
    timeout = max(1.0, min(timeout, 10.0))
    for url in urls:
        try:
            client_type, client, _ = _get_http_client()
            resp = client.get(url, timeout=timeout)
            if resp.status_code != 200:
                continue
            data = resp.json()
            last = _safe_float(data.get("lastPrice"))
            if last is None:
                continue
            open_p = _safe_float(data.get("openPrice"))
            high_p = _safe_float(data.get("highPrice"))
            low_p = _safe_float(data.get("lowPrice"))
            prev_close = _safe_float(data.get("prevClosePrice"))
            ch = _safe_float(data.get("priceChange"))
            chp = _safe_float(data.get("priceChangePercent"))
            volume = _safe_float(data.get("volume"))
            close_time = data.get("closeTime")
            market_date = ""
            if isinstance(close_time, (int, float)):
                dt = datetime.fromtimestamp(float(close_time) / 1000.0, tz=timezone.utc)
                market_date = dt.strftime("%Y-%m-%d %H:%M:%S")
            row = {
                "symbol": symbol_display,
                "lp": last,
                "open_price": open_p,
                "high_price": high_p,
                "low_price": low_p,
                "prev_close_price": prev_close,
                "ch": ch,
                "chp": chp,
                "volume": volume,
                "date": market_date or _quote_updated_at(),
                "updated_at": _quote_updated_at(),
            }
            return _prefer_updated_at_for_quote(row)
        except Exception:
            continue
    return None


_STOOQ_INDEX_SYMBOL_MAP: Dict[str, str] = {
    "IXIC": "^NDQ",
    "NASDAQ": "^NDQ",
    "NDX": "^NDQ",
    "SPX": "^SPX",
    "S&P500": "^SPX",
    "DJI": "^DJI",
    "DOWJONES": "^DJI",
}


def _is_sse_index_symbol(symbol: str) -> bool:
    """判断是否为上证综指常见写法。"""
    if not symbol or not isinstance(symbol, str):
        return False
    s = symbol.strip().upper()
    return s in {"000001", "000001.SH", "000001.SS", "SSE", "SSEC", "SH000001", "上证指数"}


def _fetch_quote_sse_official(symbol_display: str) -> Optional[Dict[str, Any]]:
    """上交所官方接口兜底（上证指数 000001）。"""
    if not _is_sse_index_symbol(symbol_display):
        return None
    url = "http://yunhq.sse.com.cn:32041/v1/sh1/dayk/000001?begin=-2&end=-1&period=day"
    headers = {
        "Referer": "http://www.sse.com.cn/",
        "User-Agent": "Mozilla/5.0",
        "Accept": "application/json, text/plain, */*",
    }
    try:
        client_type, client, _ = _get_http_client()
        if client_type == "curl_cffi":
            resp = client.get(url, headers=headers, timeout=10)
        else:
            resp = client.get(url, headers=headers, timeout=10)
        if resp.status_code != 200:
            return None
        data = resp.json()
        kl = data.get("kline")
        if not isinstance(kl, list) or not kl:
            return None
        last = kl[-1]
        prev = kl[-2] if len(kl) >= 2 else None
        if not isinstance(last, list) or len(last) < 5:
            return None
        open_p = _safe_float(last[1])
        high_p = _safe_float(last[2])
        low_p = _safe_float(last[3])
        close = _safe_float(last[4])
        volume = _safe_float(last[5]) if len(last) > 5 else None
        if close is None:
            return None
        prev_close = _safe_float(prev[4]) if isinstance(prev, list) and len(prev) > 4 else open_p
        ch: Optional[float] = None
        chp: Optional[float] = None
        if prev_close not in (None, 0):
            ch = round(close - float(prev_close), 6)
            chp = round(ch / float(prev_close) * 100, 4)
        date_raw = str(last[0]) if len(last) > 0 else ""
        market_date = (
            f"{date_raw[0:4]}-{date_raw[4:6]}-{date_raw[6:8]} 15:00:00"
            if re.match(r"^\d{8}$", date_raw)
            else ""
        )
        out = {
            "symbol": symbol_display,
            "lp": close,
            "open_price": open_p,
            "high_price": high_p,
            "low_price": low_p,
            "prev_close_price": prev_close,
            "ch": ch,
            "chp": chp,
            "volume": volume,
            "date": market_date or _quote_updated_at(),
            "updated_at": _quote_updated_at(),
        }
        return _prefer_updated_at_for_quote(out)
    except Exception:
        return None


def _normalize_stooq_symbol(item_id: str, symbol: str) -> str:
    """将输入 symbol 规范为 Stooq 代码。"""
    if not symbol or not isinstance(symbol, str):
        return ""
    s = symbol.strip().upper()
    if item_id == "investing_stock_global":
        if re.match(r"^[A-Z][A-Z0-9\.-]{0,9}$", s):
            us_sym = _US_STOCK_TYPO_MAP.get(s.split(".")[0], s.split(".")[0])
            return f"{us_sym.lower()}.us"
        return ""
    if item_id == "investing_index":
        return _STOOQ_INDEX_SYMBOL_MAP.get(s, "")
    if item_id == "investing_crypto":
        pair = _normalize_crypto_pair(s)
        if pair.endswith("USDT"):
            return f"{pair[:-4]}.v".lower()
    return ""


def _fetch_quote_stooq(symbol_display: str, item_id: str) -> Optional[Dict[str, Any]]:
    """Stooq 行情兜底（主要用于指数，且不依赖腾讯）。"""
    stooq_symbol = _normalize_stooq_symbol(item_id, symbol_display)
    if not stooq_symbol:
        return None
    url = f"https://stooq.com/q/l/?s={quote_plus(stooq_symbol)}&f=sd2t2ohlcv&h&e=csv"
    try:
        client_type, client, _ = _get_http_client()
        if client_type == "curl_cffi":
            resp = client.get(url, timeout=10)
        else:
            resp = client.get(url, timeout=10)
        if resp.status_code != 200:
            return None
        rows = list(csv.DictReader(io.StringIO(resp.text.strip())))
        if not rows:
            return None
        r0 = rows[0]
        close = _safe_float(r0.get("Close"))
        if close is None:
            return None
        open_p = _safe_float(r0.get("Open"))
        high_p = _safe_float(r0.get("High"))
        low_p = _safe_float(r0.get("Low"))
        volume = _safe_float(r0.get("Volume"))
        d = str(r0.get("Date") or "").strip()
        t = str(r0.get("Time") or "").strip()
        market_date = f"{d} {t}".strip() if d and d != "N/D" else ""
        out = {
            "symbol": symbol_display,
            "lp": close,
            "open_price": open_p,
            "high_price": high_p,
            "low_price": low_p,
            "prev_close_price": open_p,
            "ch": None if open_p is None else round(close - open_p, 6),
            "chp": None if open_p in (None, 0) else round((close - open_p) / open_p * 100, 4),
            "volume": volume,
            "date": market_date or _quote_updated_at(),
            "updated_at": _quote_updated_at(),
        }
        return _prefer_updated_at_for_quote(out)
    except Exception:
        return None


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
        "updated_at": _quote_updated_at(),
    }


def _quote_has_price(row: Dict[str, Any]) -> bool:
    """判断一条行情是否已有有效价格（lp 或 close 等）。"""
    lp = row.get("lp")
    if lp is not None and (isinstance(lp, (int, float)) or (isinstance(lp, str) and lp.strip())):
        return True
    close = row.get("close")
    return close is not None and (isinstance(close, (int, float)) or (isinstance(close, str) and close.strip()))


def _prefer_updated_at_for_quote(row: Dict[str, Any]) -> Dict[str, Any]:
    """
    行情时间字段规范：
    - 客户端展示时间优先 updated_at（接口更新时间）
    - 原始市场时间保留到 market_date（若存在）
    """
    if not isinstance(row, dict):
        return row
    updated_at = row.get("updated_at")
    date_val = row.get("date")
    if isinstance(updated_at, str) and updated_at.strip():
        if isinstance(date_val, str) and date_val.strip() and date_val != updated_at:
            row["market_date"] = date_val
        row["date"] = updated_at
    return row


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
        "updated_at": _quote_updated_at(),
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
            if item_id == "investing_crypto":
                crypto_row = _fetch_quote_crypto_binance(sym)
                if crypto_row is not None and _quote_has_price(crypto_row):
                    out.append(crypto_row)
                    logger.info("investing quotes: %s 通过 Binance 回退成功", sym)
                else:
                    # 加密货币不走腾讯兜底；Binance 默认停用，统一回退到 Stooq。
                    stooq_row = _fetch_quote_stooq(sym, item_id)
                    if stooq_row is not None and _quote_has_price(stooq_row):
                        out.append(stooq_row)
                        logger.info("investing quotes: %s 通过 Stooq 回退成功", sym)
                    else:
                        logger.warning("investing quotes: %s Stooq 无数据，且不使用腾讯兜底", sym)
                continue

            no_tencent_fallback = item_id in {"investing_index"}
            is_a_share = item_id == "investing_stock_global" and _is_a_share_symbol(sym)
            is_cn_stock = item_id == "investing_stock_global" and _is_china_stock_symbol(sym)
            tid = _resolve_symbol_to_investing_id(item_id, sym, exchange=exchange)
            if tid is not None:
                # 中国股票先尝试 1 分钟，若 no_data 再尝试 5 分钟和日线（仍走 Investing）
                intervals: List[Union[str, int]] = [1, 5, "D"] if is_cn_stock else [1]
                row = _quote_from_history_with_fallback(sym, tid, from_date, to_date, intervals)
                if row is not None:
                    out.append(_prefer_updated_at_for_quote(row))
                    if is_cn_stock:
                        logger.info("investing quotes: 中国股票 %s 通过 Investing 获取", sym)
                    continue
                if is_cn_stock:
                    logger.warning("investing quotes: 中国股票 %s Investing 无数据，回退本地源", sym)
            elif not is_a_share:
                logger.warning("investing quotes: no id for symbol=%s", sym)

            if is_a_share:
                code = _normalize_a_share_code(sym)
                if code:
                    row = _fetch_a_share_quote_akshare(sym, code)
                    if row is not None:
                        out.append(_prefer_updated_at_for_quote(row))
                        logger.info("investing quotes: A股 %s 通过 AKShare 回退成功", sym)
                    else:
                        logger.warning("investing quotes: A股 %s AKShare 无数据", sym)
                else:
                    logger.warning("investing quotes: invalid A-share symbol=%s", sym)
                if out and out[-1].get("symbol") == sym:
                    continue

            if no_tencent_fallback:
                sse_row = _fetch_quote_sse_official(sym)
                if sse_row is not None and _quote_has_price(sse_row):
                    out.append(sse_row)
                    logger.info("investing quotes: %s 通过上交所官方接口回退成功", sym)
                    continue
                stooq_row = _fetch_quote_stooq(sym, item_id)
                if stooq_row is not None and _quote_has_price(stooq_row):
                    out.append(stooq_row)
                    logger.info("investing quotes: %s 通过 Stooq 回退成功", sym)
                    continue
                logger.warning("investing quotes: %s 不使用腾讯兜底", sym)
                continue

            # 非 A 股，或 A 股 AKShare 回退失败：统一走腾讯行情兜底
            tx_row = _fetch_quote_tencent(sym, item_id)
            if tx_row is not None and _quote_has_price(tx_row):
                out.append(_prefer_updated_at_for_quote(tx_row))
                logger.info("investing quotes: %s 通过腾讯行情回退成功", sym)
            else:
                logger.warning("investing quotes: %s 腾讯行情回退失败", sym)
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
    - 若提供 symbol + from_date + to_date 且为中国股票：先用 Investing，A 股失败再回退 AKShare。
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

    # 中国股票历史：symbol + from_date + to_date（无 investing_id 时）
    symbol_param = (params.get("symbol") or "").strip()
    if not pid and symbol_param and from_date and to_date and item_id == "investing_stock_global":
        if _is_china_stock_symbol(symbol_param):
            is_a_share = _is_a_share_symbol(symbol_param)
            interval = (params.get("interval") or "D").strip()
            try:
                interval = int(interval)
            except ValueError:
                interval = interval.upper()
            tid = _resolve_symbol_to_investing_id(
                item_id, symbol_param, exchange=(params.get("exchange") or "").strip()
            )
            if tid is not None:
                content, err = fetch_investing_historical_cached(
                    tid, from_date, to_date, interval=interval
                )
                if err is None and content:
                    logger.info("investing 历史: 中国股票 %s 通过 Investing 获取", symbol_param)
                    return content or [], None
                logger.warning("investing 历史: 中国股票 %s Investing 无数据", symbol_param)
            if is_a_share:
                code = _normalize_a_share_code(symbol_param)
                if code:
                    content, err = _fetch_a_share_historical_akshare(
                        code, from_date, to_date, str(interval)
                    )
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
