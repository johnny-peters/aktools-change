# -*- coding:utf-8 -*-
# /usr/bin/env python
"""
Date: 2024/1/12 22:05
Desc: HTTP 模式主文件
"""
import json
import logging
import os
import re
import time
import urllib.parse
from logging.handlers import TimedRotatingFileHandler
from threading import Lock
from typing import Any, Dict, Tuple, Optional

import akshare as ak
from fastapi import APIRouter
from fastapi import Depends, status
from fastapi import Request
from fastapi.responses import JSONResponse, HTMLResponse
from fastapi.templating import Jinja2Templates

from aktools.core.investing import fetch_investing_data, is_investing_item
from aktools.datasets import get_pyscript_html, get_template_path
from aktools.login.user_login import User, get_current_active_user

app_core = APIRouter()

# 创建一个日志记录器
logger = logging.getLogger(name='AKToolsLog')
logger.setLevel(logging.INFO)

# 创建一个TimedRotatingFileHandler来进行日志轮转
handler = TimedRotatingFileHandler(
    filename='/tmp/aktools_log.log' if os.getenv('VERCEL') == '1' else 'aktools_log.log',
        when='midnight', interval=1, backupCount=7, encoding='utf-8'
)
formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
handler.setFormatter(formatter)
logger.addHandler(handler)

# 使用日志记录器记录信息
logger.info('这是一个信息级别的日志消息')

CACHE_TTL_SECONDS = 60
_cache_lock = Lock()
_cache: Dict[Tuple[str, str], Dict[str, Any]] = {}


def _normalize_a_share_symbol_for_cache(symbol: str) -> str:
    """600519.SH / 002340.SZ -> 600519 / 002340，用于缓存 key 统一。"""
    if not symbol:
        return symbol
    m = re.match(r"^(\d{6})(\.(SH|SZ))?$", symbol.strip().upper())
    return m.group(1) if m else symbol


def _make_cache_key(item_id: str, request: Request) -> Tuple[str, str]:
    if is_investing_item(item_id):
        query_items = list(request.query_params.items())
        if query_items:
            query_key = urllib.parse.urlencode(sorted(query_items))
        else:
            query_key = ""
        return item_id, query_key
    symbol = (request.query_params.get("symbol") or "").strip()
    symbol = _normalize_a_share_symbol_for_cache(symbol)
    return item_id, symbol


def _get_cached_content(cache_key: Tuple[str, str], allow_stale: bool = False) -> Optional[Any]:
    with _cache_lock:
        entry = _cache.get(cache_key)
        if not entry:
            return None
        if allow_stale:
            return entry["content"]
        if time.time() - entry["timestamp"] <= CACHE_TTL_SECONDS:
            return entry["content"]
        return None


def _set_cache_content(cache_key: Tuple[str, str], content: Any) -> None:
    with _cache_lock:
        _cache[cache_key] = {
            "timestamp": time.time(),
            "content": content,
        }


def _fetch_akshare_data(item_id: str, eval_str: str, has_params: bool) -> Tuple[Optional[Any], Optional[Exception]]:
    try:
        if has_params:
            received_df = eval("ak." + item_id + f"({eval_str})")
        else:
            received_df = eval("ak." + item_id + "()")
        if received_df is None:
            return None, ValueError("empty")
        temp_df = received_df.to_json(orient="records", date_format="iso")
        return json.loads(temp_df), None
    except KeyError as e:
        return None, e
    except Exception as e:
        return None, e


@app_core.get("/private/{item_id}", description="私人接口", summary="该接口主要提供私密访问来获取数据")
def root(
        request: Request,
        item_id: str,
        current_user: User = Depends(get_current_active_user),
):
    """
    接收请求参数及接口名称并返回 JSON 数据
    此处由于 AKShare 的请求中是同步模式，所以这边在定义 root 函数中没有使用 asyncio 来定义，这样可以开启多线程访问
    :param request: 请求信息
    :type request: Request
    :param item_id: 必选参数; 测试接口名 ak.stock_dxsyl_em() 来获取 打新收益率 数据
    :type item_id: str
    :param current_user: 依赖注入，为了进行用户的登录验证
    :type current_user: str
    :return: 指定 接口名称 和 参数 的数据
    :rtype: json
    """
    cache_key = _make_cache_key(item_id, request)
    cached_content = _get_cached_content(cache_key)
    if cached_content is not None:
        return JSONResponse(status_code=status.HTTP_200_OK, content=cached_content)

    interface_list = dir(ak)
    decode_params = urllib.parse.unquote(str(request.query_params))

    if is_investing_item(item_id):
        params = {k: (v or "") for k, v in request.query_params.items()}
        content, error = fetch_investing_data(item_id, params)
        if error is None and content is not None:
            _set_cache_content(cache_key, content)
            return JSONResponse(status_code=status.HTTP_200_OK, content=content)
        cached_content = _get_cached_content(cache_key, allow_stale=True)
        if cached_content is not None:
            return JSONResponse(status_code=status.HTTP_200_OK, content=cached_content)
        err_msg = str(error) if error else "数据为空"
        return JSONResponse(
            status_code=status.HTTP_502_BAD_GATEWAY,
            content={"error": f"Investing 数据拉取失败: {err_msg}"},
        )

    if item_id not in interface_list:
        cached_content = _get_cached_content(cache_key, allow_stale=True)
        if cached_content is not None:
            return JSONResponse(status_code=status.HTTP_200_OK, content=cached_content)
        return JSONResponse(
            status_code=status.HTTP_404_NOT_FOUND,
            content={
                "error": "未找到该接口，请升级 AKShare 到最新版本并在文档中确认该接口的使用方式：https://akshare.akfamily.xyz"
            },
        )
    eval_str = decode_params.replace("&", '", ').replace("=", '="') + '"'
    eval_str = re.sub(r'symbol="(\d{6})\.(SH|SZ)"', r'symbol="\1"', eval_str, flags=re.IGNORECASE)
    has_params = bool(request.query_params)
    content, error = _fetch_akshare_data(item_id, eval_str, has_params)
    if error is None and content is not None:
        _set_cache_content(cache_key, content)
        return JSONResponse(status_code=status.HTTP_200_OK, content=content)

    cached_content = _get_cached_content(cache_key, allow_stale=True)
    if cached_content is not None:
        return JSONResponse(status_code=status.HTTP_200_OK, content=cached_content)

    if isinstance(error, KeyError):
        return JSONResponse(
            status_code=status.HTTP_404_NOT_FOUND,
            content={
                "error": f"请输入正确的参数错误 {error}，请升级 AKShare 到最新版本并在文档中确认该接口的使用方式：https://akshare.akfamily.xyz"
            },
        )

    if isinstance(error, ValueError):
        return JSONResponse(
            status_code=status.HTTP_404_NOT_FOUND,
            content={"error": "该接口返回数据为空，请确认参数是否正确：https://akshare.akfamily.xyz"},
        )

    logger.error("接口处理失败: %s - %s", item_id, error, exc_info=True)
    err_hint = "接口处理失败，请稍后重试"
    if error and "Connection" in type(error).__name__:
        err_hint = "数据源连接失败，可能是网络问题，请检查网络后重试"
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={"error": err_hint},
    )


@app_core.get(path="/public/{item_id}", description="公开接口", summary="该接口主要提供公开访问来获取数据")
def root(request: Request, item_id: str):
    """
    接收请求参数及接口名称并返回 JSON 数据
    此处由于 AKShare 的请求中是同步模式，所以这边在定义 root 函数中没有使用 asyncio 来定义，这样可以开启多线程访问
    :param request: 请求信息
    :type request: Request
    :param item_id: 必选参数; 测试接口名 stock_dxsyl_em 来获取 打新收益率 数据
    :type item_id: str
    :return: 指定 接口名称 和 参数 的数据
    :rtype: json
    """
    cache_key = _make_cache_key(item_id, request)
    cached_content = _get_cached_content(cache_key)
    if cached_content is not None:
        logger.info(f"命中缓存: {item_id}")
        return JSONResponse(status_code=status.HTTP_200_OK, content=cached_content)

    if is_investing_item(item_id):
        params = {k: (v or "") for k, v in request.query_params.items()}
        content, error = fetch_investing_data(item_id, params)
        if error is None and content is not None:
            _set_cache_content(cache_key, content)
            logger.info(f"获取到 Investing {item_id} 的数据")
            return JSONResponse(status_code=status.HTTP_200_OK, content=content)
        cached_content = _get_cached_content(cache_key, allow_stale=True)
        if cached_content is not None:
            logger.info(f"Investing 抓取失败，返回缓存: {item_id}")
            return JSONResponse(status_code=status.HTTP_200_OK, content=cached_content)
        err_msg = str(error) if error else "数据为空"
        logger.info(f"Investing 数据拉取失败: {item_id} - {err_msg}")
        return JSONResponse(
            status_code=status.HTTP_502_BAD_GATEWAY,
            content={"error": f"Investing 数据拉取失败: {err_msg}"},
        )

    interface_list = dir(ak)
    decode_params = urllib.parse.unquote(str(request.query_params))
    if item_id not in interface_list:
        logger.info("未找到该接口，请升级 AKShare 到最新版本并在文档中确认该接口的使用方式：https://akshare.akfamily.xyz")
        cached_content = _get_cached_content(cache_key, allow_stale=True)
        if cached_content is not None:
            logger.info(f"接口不可用，返回缓存: {item_id}")
            return JSONResponse(status_code=status.HTTP_200_OK, content=cached_content)
        return JSONResponse(
            status_code=status.HTTP_404_NOT_FOUND,
            content={
                "error": "未找到该接口，请升级 AKShare 到最新版本并在文档中确认该接口的使用方式：https://akshare.akfamily.xyz"
            },
        )
    if "cookie" in decode_params:
        eval_str = (
                decode_params.split(sep="=", maxsplit=1)[0]
                + "='"
                + decode_params.split(sep="=", maxsplit=1)[1]
                + "'"
        )
        eval_str = eval_str.replace("+", " ")
    else:
        eval_str = decode_params.replace("&", '", ').replace("=", '="') + '"'
        eval_str = eval_str.replace("+", " ")  # 处理传递的参数中带空格的情况
    # A 股 symbol 规范：600519.SH / 002340.SZ -> 600519 / 002340（AKShare 需纯 6 位代码）
    eval_str = re.sub(r'symbol="(\d{6})\.(SH|SZ)"', r'symbol="\1"', eval_str, flags=re.IGNORECASE)
    has_params = bool(request.query_params)
    content, error = _fetch_akshare_data(item_id, eval_str, has_params)
    if error is None and content is not None:
        _set_cache_content(cache_key, content)
        logger.info(f"获取到 {item_id} 的数据")
        return JSONResponse(status_code=status.HTTP_200_OK, content=content)

    cached_content = _get_cached_content(cache_key, allow_stale=True)
    if cached_content is not None:
        logger.info(f"抓取失败，返回缓存: {item_id}")
        return JSONResponse(status_code=status.HTTP_200_OK, content=cached_content)

    if isinstance(error, KeyError):
        logger.info(
            f"请输入正确的参数错误 {error}，请升级 AKShare 到最新版本并在文档中确认该接口的使用方式：https://akshare.akfamily.xyz")
        return JSONResponse(
            status_code=status.HTTP_404_NOT_FOUND,
            content={
                "error": f"请输入正确的参数错误 {error}，请升级 AKShare 到最新版本并在文档中确认该接口的使用方式：https://akshare.akfamily.xyz"
            },
        )

    if isinstance(error, ValueError):
        logger.info("该接口返回数据为空，请确认参数是否正确：https://akshare.akfamily.xyz")
        return JSONResponse(
            status_code=status.HTTP_404_NOT_FOUND,
            content={"error": "该接口返回数据为空，请确认参数是否正确：https://akshare.akfamily.xyz"},
        )

    logger.error("接口处理失败: %s - %s", item_id, error, exc_info=True)
    err_hint = "接口处理失败，请稍后重试"
    if error and "Connection" in type(error).__name__:
        err_hint = "数据源连接失败，可能是网络问题，请检查网络后重试"
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={"error": err_hint},
    )


def generate_html_response():
    file_path = get_pyscript_html(file="akscript.html")
    with open(file_path, encoding="utf8") as f:
        html_content = f.read()
    return HTMLResponse(content=html_content, status_code=200)


short_path = get_template_path()
templates = Jinja2Templates(directory=short_path)


@app_core.get(
    path="/show-temp/{interface}",
    response_class=HTMLResponse,
    description="展示 PyScript",
    summary="该接口主要展示 PyScript 游览器运行 Python 代码",
)
def akscript_temp(request: Request, interface: str):
    return templates.TemplateResponse(
        "akscript.html",
        context={
            "request": request,
            "ip": request.headers["host"],
            "interface": interface,
        },
    )


@app_core.get(
    path="/show",
    response_class=HTMLResponse,
    description="展示 PyScript",
    summary="该接口主要展示 PyScript 游览器运行 Python 代码",
)
def akscript():
    return generate_html_response()
