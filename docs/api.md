# AKTools API 文档

本文档描述 AKTools 服务部署后所有可用的 HTTP API，包括请求 URL、参数、返回格式及使用说明。

**说明**：将 `{BASE_URL}` 替换为你的服务地址，例如 `http://127.0.0.1:8080` 或 `https://your-domain.com`。

---

## 一、系统与通用接口

### 1. 网站首页

| 项目         | 说明              |
| ------------ | ----------------- |
| **请求 URL** | `GET {BASE_URL}/` |
| **认证**     | 不需要            |
| **请求参数** | 无                |

**返回**：HTML 页面（首页，展示 AKTools/AKShare 版本等信息）。

---

### 2. 获取版本信息

| 项目         | 说明                     |
| ------------ | ------------------------ |
| **请求 URL** | `GET {BASE_URL}/version` |
| **认证**     | 不需要                   |
| **请求参数** | 无                       |

**返回数据模板**（JSON）：

```json
{
  "ak_current_version": "1.12.0",
  "at_current_version": "0.0.81",
  "ak_latest_version": "1.12.0",
  "at_latest_version": "0.0.81"
}
```

| 字段               | 类型   | 说明                    |
| ------------------ | ------ | ----------------------- |
| ak_current_version | string | 当前安装的 AKShare 版本 |
| at_current_version | string | 当前安装的 AKTools 版本 |
| ak_latest_version  | string | 最新 AKShare 版本       |
| at_latest_version  | string | 最新 AKTools 版本       |

**使用说明**：用于检查服务端 AKShare/AKTools 版本，便于客户端兼容或升级判断。

---

### 3. Favicon

| 项目         | 说明                         |
| ------------ | ---------------------------- |
| **请求 URL** | `GET {BASE_URL}/favicon.ico` |
| **认证**     | 不需要                       |
| **请求参数** | 无                           |

**返回**：二进制文件（favicon.ico）。

---

## 二、认证接口

### 4. 获取 Token（登录）

用于获取访问**私人数据接口**所需的 Bearer Token。

| 项目             | 说明                                |
| ---------------- | ----------------------------------- |
| **请求 URL**     | `POST {BASE_URL}/auth/token`        |
| **Content-Type** | `application/x-www-form-urlencoded` |
| **请求参数**     | 表单字段（Body）                    |

**请求参数**（表单）：

| 参数名   | 类型   | 必填 | 说明   |
| -------- | ------ | ---- | ------ |
| username | string | 是   | 用户名 |
| password | string | 是   | 密码   |

**默认演示账号**（以项目内置伪数据库为准）：

- 用户名：`akshare`
- 密码：`akfamily`

**成功返回**（200，JSON）：

```json
{
  "access_token": "akshare",
  "token_type": "bearer"
}
```

| 字段         | 类型   | 说明                        |
| ------------ | ------ | --------------------------- |
| access_token | string | 用于后续请求的 Bearer Token |
| token_type   | string | 固定为 `bearer`             |

**失败返回**（400，JSON）：

```json
{
  "detail": "Incorrect username or password"
}
```

**使用说明**：

- 调用私人接口时，在请求头中加上：`Authorization: Bearer {access_token}`。
- 当前版本为演示用伪数据库，生产环境需替换为真实用户存储（如数据库）。

---

## 三、数据接口（AKShare 与 Investing）

AKTools 支持两类数据源：  
1）**[AKShare](https://akshare.akfamily.xyz/)**：`item_id` 与 AKShare 模块中的函数名一致（如 `stock_zh_a_hist`、`stock_dxsyl_em` 等），完整列表与参数以 [AKShare 官方文档](https://akshare.akfamily.xyz/) 为准。  
2）**Investing（cn.investing.com）**：`item_id` 为固定名称（如 `investing_index`、`investing_stock_global` 等），见下方「三B、Investing 数据接口」。

### 5. 公开数据接口（无需登录）

| 项目           | 说明                                            |
| -------------- | ----------------------------------------------- |
| **请求 URL**   | `GET {BASE_URL}/api/public/{item_id}`           |
| **认证**       | 不需要                                          |
| **路径参数**   | item_id：AKShare 接口名（如 `stock_zh_a_hist`） |
| **Query 参数** | 与对应 AKShare 函数参数一致，见下方说明         |

**请求示例**：

- 无参数接口：  
  `GET {BASE_URL}/api/public/stock_comment_em`
- 带参数接口：  
  `GET {BASE_URL}/api/public/stock_zh_a_hist?symbol=000001&period=daily&start_date=20231101&end_date=20231201&adjust=qfq`

**成功返回**（200）：  
JSON 数组，每项为一行数据（与 AKShare 返回的 DataFrame 行一致），例如：

```json
[
  {
    "日期": "2023-11-01T00:00:00",
    "开盘": 10.5,
    "收盘": 10.8,
    "最高": 11.0,
    "最低": 10.4,
    "成交量": 1000000,
    "成交额": 10800000,
    "振幅": 5.2,
    "涨跌幅": 2.5,
    "涨跌额": 0.26,
    "换手率": 1.2
  }
]
```

具体字段以 AKShare 各接口文档为准。

**错误返回**（404，JSON）：

```json
{
  "error": "未找到该接口，请升级 AKShare 到最新版本并在文档中确认该接口的使用方式：https://akshare.akfamily.xyz"
}
```

或：

```json
{
  "error": "该接口返回数据为空，请确认参数是否正确：https://akshare.akfamily.xyz"
}
```

或：

```json
{
  "error": "请输入正确的参数错误 {具体错误}，请升级 AKShare 到最新版本并在文档中确认该接口的使用方式：https://akshare.akfamily.xyz"
}
```

**使用说明**：

- **item_id**：必须为 AKShare 中存在的函数名（如 `stock_zh_a_hist`、`stock_dxsyl_em`），可在 [AKShare 文档](https://akshare.akfamily.xyz/) 中查找。
- **参数传递**：通过 URL Query 传递，格式为 `参数名=参数值`，多个参数用 `&` 连接；不要给参数值加引号。
- **特殊参数 cookie**：若接口需要 cookie，将整个 cookie 字符串作为 `cookie=xxx` 传递。
- 参数名、参数个数和含义以 AKShare 对应接口文档为准。
- **热点缓存**：以 `api + symbol` 作为缓存 key；同一请求在 1 分钟内直接返回缓存；爬取失败时返回最近一次缓存（若有）。

---

### 6. 私人数据接口（需要登录）

| 项目           | 说明                                   |
| -------------- | -------------------------------------- |
| **请求 URL**   | `GET {BASE_URL}/api/private/{item_id}` |
| **认证**       | 需要 Bearer Token（见「获取 Token」）  |
| **请求头**     | `Authorization: Bearer {access_token}` |
| **路径参数**   | item_id：AKShare 接口名                |
| **Query 参数** | 与对应 AKShare 函数参数一致            |

**请求示例**：

```http
GET {BASE_URL}/api/private/stock_zh_a_hist?symbol=000001&period=daily
Authorization: Bearer akshare
```

**返回数据模板**：  
与「公开数据接口」相同——成功为 JSON 数组，错误为带 `error` 的 JSON（404 等）。

**使用说明**：

- 除需在 Header 中带 `Authorization: Bearer {access_token}` 外，URL、参数、返回格式与公开接口一致。
- 未带 Token 或 Token 无效会返回 401。
- **热点缓存**：以 `api + symbol` 作为缓存 key；同一请求在 1 分钟内直接返回缓存；爬取失败时返回最近一次缓存（若有）。

---

## 三B、Investing 数据接口（cn.investing.com）

数据来源为 [cn.investing.com](https://cn.investing.com/)（通过 [investiny](https://pypi.org/project/investiny/) 访问公开数据，无需登录）。  
与 AKShare 接口共用同一 URL 形式：`GET {BASE_URL}/api/public/{item_id}` 或 `GET {BASE_URL}/api/private/{item_id}`，**item_id** 使用下表所列的固定名称。  
**限频与缓存**：建议控制请求频率，避免封禁；服务端对同一请求（api + symbol/investing_id）有 1 分钟热点缓存，爬取失败时会返回最近一次成功缓存（若有）。

### Investing 接口清单

| item_id                | 说明                       |
| ---------------------- | -------------------------- |
| investing_index        | 指数                       |
| investing_stock_global | 全球股票（A 股、港股以外） |
| investing_futures      | 期货                       |
| investing_fx           | 货币（外汇）               |
| investing_etf          | 交易所交易基金             |
| investing_bond         | 国债                       |
| investing_fund         | 基金                       |
| investing_crypto       | 虚拟货币                   |

### Investing 官网页面与 API 对应

| 官网页面 | 链接 | 对应 item_id |
| -------- | ---- | ------------ |
| 虚拟货币 | [cn.investing.com/crypto/currencies](https://cn.investing.com/crypto/currencies) | `investing_crypto` |
| 热门股票 | [cn.investing.com/equities/trending-stocks](https://cn.investing.com/equities/trending-stocks) | `investing_stock_global` |
| 主要指数 | [cn.investing.com/indices/major-indices](https://cn.investing.com/indices/major-indices) | `investing_index` |

**示例**：要拉取「主要指数」页面对应的列表，可请求 `GET {BASE_URL}/api/public/investing_index?limit=20`；要拉取「虚拟货币」实时行情，可请求 `GET {BASE_URL}/api/public/investing_crypto?symbols=BTC,ETH`。

### 三种用法

1. **拉取实时行情（quotes）**  
   传入 `symbols` 时，先按类型将资产名称/代码解析为 Investing 的 investing_id，再取**最近一分钟 K 线**拼出含最新价、涨跌等字段的实时行情。

   **Query 参数**：

   | 参数名    | 类型   | 必填 | 说明                           |
   | --------- | ------ | ---- | ------------------------------ |
   | symbols   | string | 是   | 资产代码，多个用英文逗号分隔   |
   | exchange  | string | 否   | 交易所（解析时用于精确匹配）   |

   **请求示例**：

   ```http
   GET {BASE_URL}/api/public/investing_stock_global?symbols=AAPL
   GET {BASE_URL}/api/public/investing_stock_global?symbols=AAPL,MSFT
   GET {BASE_URL}/api/public/investing_crypto?symbols=BTC,ETH
   ```

   **返回**：JSON 数组，每项为一条实时行情（含 `symbol` 及行情字段，如 `lp` 最新价、`ch` 涨跌、`chp` 涨跌幅等）。若某代码无法解析为 investing_id，则不会出现在结果中。

2. **拉取资产列表（搜索）**  
   不传 `investing_id`、`from_date`、`to_date` 时，按类型返回资产列表（来自 Investing 搜索）。

   **Query 参数**：

   | 参数名   | 类型   | 必填 | 说明                       |
   | -------- | ------ | ---- | -------------------------- |
   | query    | string | 否   | 搜索关键词，空则用类型默认 |
   | limit    | number | 否   | 条数，默认 50，最大 200    |
   | exchange | string | 否   | 交易所，如 NASDAQ          |

   **请求示例**：

   ```http
   GET {BASE_URL}/api/public/investing_index?limit=10
   GET {BASE_URL}/api/public/investing_stock_global?query=AAPL&limit=5
   GET {BASE_URL}/api/public/investing_crypto?limit=20

   http://127.0.0.1:8080/api/public/investing_index?symbols=SSEC
   http://127.0.0.1:8080/api/public/investing_stock_global?symbols=AAPL
   http://127.0.0.1:8080/api/public/investing_futures?symbols=CL
   http://127.0.0.1:8080/api/public/investing_fx?symbols=EURUSD
   http://127.0.0.1:8080/api/public/investing_etf?symbols=SPY
   http://127.0.0.1:8080/api/public/investing_bond?symbols=US10Y
   http://127.0.0.1:8080/api/public/investing_fund?symbols=SPY
   http://127.0.0.1:8080/api/public/investing_crypto?symbols=ETH
   ```

   **返回**：JSON 数组，每项为一条资产信息（含 `ticker` 等，可用于历史接口的 `investing_id`）。

3. **拉取历史数据**  
   同时传入 `investing_id`、`from_date`、`to_date` 时，返回该资产在指定日期范围内的历史数据。

   **Query 参数**：

   | 参数名       | 类型          | 必填 | 说明                                              |
   | ------------ | ------------- | ---- | ------------------------------------------------- |
   | investing_id | string        | 是   | Investing 资产 ID（可从列表接口的 `ticker` 取得） |
   | from_date    | string        | 是   | 开始日期，支持 `YYYYMMDD` 或 `YYYY-MM-DD`         |
   | to_date      | string        | 是   | 结束日期，格式同上                                |
   | interval     | string/number | 否   | 周期：`D`/`W`/`M` 或 `1/5/15/30/60/300`（分钟）   |

   **请求示例**：

   ```http
   GET {BASE_URL}/api/public/investing_stock_global?investing_id=6408&from_date=2024-01-01&to_date=2024-01-10
   GET {BASE_URL}/api/public/investing_stock_global?investing_id=6408&from_date=2024-01-01&to_date=2024-01-10&interval=D
   ```

   **返回**：JSON 数组，每项为一条 K 线/行情记录（字段以 Investing 返回为准，如开高低收等）。

### 错误返回

- **502**：Investing 数据拉取失败（如依赖未安装、网络或上游不可用）。若存在历史缓存，会改为返回 200 与缓存内容。
- **404**：仅当 item_id 既不是 AKShare 接口名也不是上表所列 Investing 接口名时返回。

### Investing 数据日期说明

- 实时行情使用**一分钟 K 线**，`date` 字段格式为 `MM/DD/YYYY HH:MM`（含时分，不含秒）。
- 历史数据当 `interval=D/W/M` 时仅含日期；当 `interval` 为分钟级时含 `HH:MM`。

### Investing 示例

```bash
# 实时行情（单标的 / 多标的）
curl "{BASE_URL}/api/public/investing_stock_global?symbols=AAPL"
curl "{BASE_URL}/api/public/investing_stock_global?symbols=AAPL,MSFT"

# 指数列表（前 10 条）
curl "{BASE_URL}/api/public/investing_index?limit=10"

# 全球股票搜索 AAPL
curl "{BASE_URL}/api/public/investing_stock_global?query=AAPL&limit=5"

# 某资产历史（investing_id 6408 示例）
curl "{BASE_URL}/api/public/investing_stock_global?investing_id=6408&from_date=2024-01-01&to_date=2024-01-10"

# 虚拟货币列表
curl "{BASE_URL}/api/public/investing_crypto?limit=20"
```

---

## 四、页面与展示接口

### 7. PyScript 展示页（默认）

| 项目         | 说明                      |
| ------------ | ------------------------- |
| **请求 URL** | `GET {BASE_URL}/api/show` |
| **认证**     | 不需要                    |
| **请求参数** | 无                        |

**返回**：HTML 页面（PyScript 浏览器运行 Python 代码的展示页）。

---

### 8. PyScript 模板页（带接口名）

| 项目         | 说明                                       |
| ------------ | ------------------------------------------ |
| **请求 URL** | `GET {BASE_URL}/api/show-temp/{interface}` |
| **认证**     | 不需要                                     |
| **路径参数** | interface：接口名，会传入模板上下文        |

**返回**：HTML 页面（PyScript 模板，页面内可使用当前 `interface` 等信息）。

---

## 五、常用 AKShare 接口示例

以下为常见接口名及典型参数，具体以 [AKShare 文档](https://akshare.akfamily.xyz/) 为准。

| 接口名 (item_id)   | 说明                        | 常用 Query 参数示例                                                          |
| ------------------ | --------------------------- | ---------------------------------------------------------------------------- |
| stock_zh_a_hist    | A 股历史 K 线（日/周/月线） | symbol=000001, **period**=daily/weekly/monthly, start_date, end_date, adjust |
| stock_dxsyl_em     | 打新收益率                  | 无参或见文档                                                                 |
| stock_comment_em   | 千股千评                    | 无                                                                           |
| fund_etf_spot_em   | 沪深 ETF 实时行情           | 无                                                                           |
| stock_zh_a_spot_em | A 股实时行情                | 无                                                                           |

**示例**：

```bash
# 获取平安银行(000001)日 K 线
curl "http://127.0.0.1:8080/api/public/stock_zh_a_hist?symbol=000001&period=daily"

# 周线、月线：仅改 period 即可
curl "http://127.0.0.1:8080/api/public/stock_zh_a_hist?symbol=000001&period=weekly"
curl "http://127.0.0.1:8080/api/public/stock_zh_a_hist?symbol=000001&period=monthly"

# 获取千股千评（无参数）
curl "http://127.0.0.1:8080/api/public/stock_comment_em"
```

---

## 六、汇总表

| 序号 | 方法 | URL                        | 认证   | 说明                          |
| ---- | ---- | -------------------------- | ------ | ----------------------------- |
| 1    | GET  | /                          | 否     | 网站首页                      |
| 2    | GET  | /version                   | 否     | 版本信息                      |
| 3    | GET  | /favicon.ico               | 否     | Favicon                       |
| 4    | POST | /auth/token                | 否     | 获取 Token                    |
| 5    | GET  | /api/public/{item_id}      | 否     | 公开 AKShare / Investing 数据 |
| 6    | GET  | /api/private/{item_id}     | Bearer | 私人 AKShare / Investing 数据 |
| 7    | GET  | /api/show                  | 否     | PyScript 展示页               |
| 8    | GET  | /api/show-temp/{interface} | 否     | PyScript 模板页               |

---

## 七、参考链接

- [AKShare 官方文档](https://akshare.akfamily.xyz/)：查询所有可用接口名及参数、返回值含义。
- [AKTools 中文文档](https://aktools.readthedocs.io/)：安装、启动及使用说明。
- [Investing.com](https://cn.investing.com/) / [investiny](https://pypi.org/project/investiny/)：Investing 数据接口依赖与数据来源说明。
