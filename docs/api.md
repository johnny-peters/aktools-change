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

## 三、数据接口（AKShare 动态接口）

AKTools 将 [AKShare](https://akshare.akfamily.xyz/) 的 Python 接口暴露为 HTTP API。  
可用接口名与 AKShare 模块中的函数名一致（如 `stock_zh_a_hist`、`stock_dxsyl_em` 等），完整列表与参数以 [AKShare 官方文档](https://akshare.akfamily.xyz/) 为准。

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

| 序号 | 方法 | URL                        | 认证   | 说明              |
| ---- | ---- | -------------------------- | ------ | ----------------- |
| 1    | GET  | /                          | 否     | 网站首页          |
| 2    | GET  | /version                   | 否     | 版本信息          |
| 3    | GET  | /favicon.ico               | 否     | Favicon           |
| 4    | POST | /auth/token                | 否     | 获取 Token        |
| 5    | GET  | /api/public/{item_id}      | 否     | 公开 AKShare 数据 |
| 6    | GET  | /api/private/{item_id}     | Bearer | 私人 AKShare 数据 |
| 7    | GET  | /api/show                  | 否     | PyScript 展示页   |
| 8    | GET  | /api/show-temp/{interface} | 否     | PyScript 模板页   |

---

## 七、参考链接

- [AKShare 官方文档](https://akshare.akfamily.xyz/)：查询所有可用接口名及参数、返回值含义。
- [AKTools 中文文档](https://aktools.readthedocs.io/)：安装、启动及使用说明。
