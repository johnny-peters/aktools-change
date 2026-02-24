# Investing 数据流程梳理

## 一、现有流程

### 1. 数据流向

```
用户请求 (GET /api/public/investing_xxx?investing_id=6408&from_date=...&to_date=...)
    ↓
api.py root() 
    ↓
_make_cache_key(item_id, request)  → (item_id, "investing_id=6408&from_date=2024-02-01&to_date=2024-04-30")
    ↓
_get_cached_content(cache_key)  → 命中？
    ├── 命中（1 分钟内相同请求）→ 直接返回缓存
    └── 未命中 ↓
fetch_investing_data(item_id, params)
    ↓
fetch_investing_historical(investing_id, from_date, to_date, interval)
    ↓
_request_to_investing("history", params)  → 爬取 [from_date, to_date] 全量
    ↓
_set_cache_content(cache_key, content)  → 按「完整请求参数」写入缓存
    ↓
返回给用户
```

### 2. 缓存机制

| 项目 | 现状 |
|------|------|
| **缓存 key** | `(item_id, 完整 query_string)`，例如 `("investing_stock_global", "from_date=2024-01-01&investing_id=6408&to_date=2024-03-31")` |
| **粒度** | 按「请求参数」粒度：不同 from_date/to_date = 不同 key |
| **TTL** | 60 秒 |
| **存储** | 内存字典 `_cache: Dict[Tuple[str,str], {timestamp, content}]` |

### 3. 现状问题（与预期不符）

- **无缓存**：会爬取整段区间后返回 ✓ 符合预期
- **全量命中**：请求 2、3 月，缓存里正好有 2、3 月 → 直接返回 ✓ 符合预期
- **部分命中**：  
  - 缓存：1、2、3 月  
  - 用户请求：2、3、4 月  
  - 现状：cache key 不同，视为未命中，会**重新爬取 2、3、4 月整段**，而不是只爬 4 月再合并 ✗ 不符合预期

## 二、预期行为

1. **无缓存**：爬取 [from_date, to_date]，写入缓存，返回
2. **全量命中**：缓存覆盖用户区间 → 直接返回
3. **部分命中**：
   - 分析用户区间与缓存区间的交集、差集
   - 仅爬取**未覆盖**的区间
   - 合并：新数据 + 现有缓存 → 更新缓存为更大区间（如 1–4 月）
   - 从合并结果中筛选用户请求的区间返回

## 三、已实现逻辑（investing.py）

1. **历史缓存**：`_HISTORICAL_CACHE` 按 `(investing_id, interval)` 存储 `{from_date, to_date, rows}`
2. **缺失区间**：`_find_missing_ranges()` 计算请求与缓存的差集，仅爬取缺失区间
3. **合并与去重**：`_merge_and_dedupe_rows()` 按日期合并，新数据优先
4. **入口**：`fetch_investing_historical_cached()` 取代原 `fetch_investing_historical` 用于历史请求

流程示例：缓存 1–3 月，用户请求 2–4 月 → 仅爬 4 月 → 合并为 1–4 月 → 返回 2–4 月。

---

*文档已更新，实现见 `aktools/core/investing.py`。*
