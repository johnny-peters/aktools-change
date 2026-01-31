# -*- coding: utf-8 -*-
"""
测试脚本：每种资产类型获取 5 条「实时行情」（通过列表接口取 5 个标的，再拉最近历史作为最新价）。
需先启动 AKTools 服务（如 python -m aktools --host 127.0.0.1 --port 8080）。
用法：python tests/fetch_investing_quotes.py [BASE_URL]
"""
import json
import os
import sys
from datetime import datetime, timedelta

try:
    import requests
except ImportError:
    print("请安装 requests: pip install requests")
    sys.exit(1)

# 与 aktools.core.investing 一致
INVESTING_ITEM_IDS = [
    "investing_index",
    "investing_stock_global",
    "investing_futures",
    "investing_fx",
    "investing_etf",
    "investing_bond",
    "investing_fund",
    "investing_crypto",
]

ASSET_TYPE_LABELS = {
    "investing_index": "指数",
    "investing_stock_global": "全球股票",
    "investing_futures": "期货",
    "investing_fx": "货币",
    "investing_etf": "交易所交易基金",
    "investing_bond": "国债",
    "investing_fund": "基金",
    "investing_crypto": "虚拟货币",
}

LIMIT = 5
TIMEOUT = 15


def main() -> None:
    base_url = (sys.argv[1] if len(sys.argv) > 1 else None) or os.getenv("AKTOOLS_BASE_URL", "http://127.0.0.1:8080")
    base_url = base_url.rstrip("/")

    today = datetime.now()
    end_date = today.strftime("%Y-%m-%d")
    start_date = (today - timedelta(days=10)).strftime("%Y-%m-%d")

    all_results = []
    for item_id in INVESTING_ITEM_IDS:
        label = ASSET_TYPE_LABELS.get(item_id, item_id)
        print(f"\n[{label}] {item_id}")

        # 1. 列表：取 5 个标的
        list_url = f"{base_url}/api/public/{item_id}"
        try:
            list_resp = requests.get(list_url, params={"limit": LIMIT}, timeout=TIMEOUT)
        except requests.RequestException as e:
            print(f"  列表请求失败: {e}")
            all_results.append({"type": label, "item_id": item_id, "error": str(e), "quotes": []})
            continue

        if list_resp.status_code != 200:
            print(f"  列表 {list_resp.status_code}: {list_resp.text[:200]}")
            all_results.append({"type": label, "item_id": item_id, "error": list_resp.text[:200], "quotes": []})
            continue

        try:
            items = list_resp.json()
        except json.JSONDecodeError:
            print("  列表返回非 JSON")
            all_results.append({"type": label, "item_id": item_id, "error": "非 JSON", "quotes": []})
            continue

        if not isinstance(items, list):
            print("  列表返回非数组")
            all_results.append({"type": label, "item_id": item_id, "error": "非数组", "quotes": []})
            continue

        quotes = []
        for i, asset in enumerate(items[:LIMIT]):
            if not isinstance(asset, dict):
                continue
            # investiny search 返回的项通常含 ticker（investing_id）、name、symbol 等
            ticker = asset.get("ticker") or asset.get("id") or asset.get("symbol")
            name = asset.get("name") or asset.get("symbol") or str(ticker) or f"#{i+1}"

            if ticker is None:
                quotes.append({"name": name, "error": "无 ticker/id"})
                continue

            try:
                tid = int(ticker)
            except (TypeError, ValueError):
                quotes.append({"name": name, "ticker": ticker, "error": "ticker 非数字"})
                continue

            # 2. 历史：取最近一段，最后一行视为「实时行情」
            try:
                hist_resp = requests.get(
                    list_url,
                    params={
                        "investing_id": str(tid),
                        "from_date": start_date,
                        "to_date": end_date,
                    },
                    timeout=TIMEOUT,
                )
            except requests.RequestException as e:
                quotes.append({"name": name, "ticker": tid, "error": str(e)})
                continue

            if hist_resp.status_code != 200:
                quotes.append({"name": name, "ticker": tid, "error": f"HTTP {hist_resp.status_code}"})
                continue

            try:
                rows = hist_resp.json()
            except json.JSONDecodeError:
                quotes.append({"name": name, "ticker": tid, "error": "历史返回非 JSON"})
                continue

            if not isinstance(rows, list) or not rows:
                quotes.append({"name": name, "ticker": tid, "error": "无历史数据"})
                continue

            last = rows[-1]
            if isinstance(last, dict):
                quote = {
                    "name": name,
                    "ticker": tid,
                    "date": last.get("date"),
                    "open": last.get("open"),
                    "high": last.get("high"),
                    "low": last.get("low"),
                    "close": last.get("close"),
                    "volume": last.get("volume"),
                }
            else:
                quote = {"name": name, "ticker": tid, "raw": last}
            quotes.append(quote)
            print(f"  {i+1}. {name} (id={tid}) close={quote.get('close')} date={quote.get('date')}")

        all_results.append({"type": label, "item_id": item_id, "quotes": quotes})

    # 汇总输出
    print("\n" + "=" * 60)
    print("汇总（JSON）")
    print("=" * 60)
    print(json.dumps(all_results, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
