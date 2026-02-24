# AKTools 打包与 Windows 运行指南

## 一、打包成 whl

### 前置条件

- Python 3.8 及以上
- 已安装项目依赖：`pip install -r requirements.txt`

### 方法一：使用 build（推荐）

```powershell
# 安装打包工具
pip install build

# 在项目根目录执行打包
cd e:\projects\aktools
python -m build
```

构建完成后，whl 文件位于 `dist/` 目录，例如：`dist/aktools-0.0.91-py3-none-any.whl`。

### 方法二：使用 setup.py

```powershell
cd e:\projects\aktools
pip install wheel
python setup.py bdist_wheel
```

whl 文件位于 `dist/` 目录。

### 方法三：仅生成源码包（sdist）

```powershell
python setup.py sdist
```

生成 `dist/aktools-0.0.91.tar.gz`，可用于 `pip install xxx.tar.gz` 安装。

---

## 二、在 Windows 平台运行

### 1. 安装 whl

```powershell
# 方式 A：安装本地 whl
pip install dist\aktools-0.0.91-py3-none-any.whl

# 方式 B：从项目目录安装（开发模式）
pip install -e .

# 方式 C：从 PyPI 安装
pip install aktools
```

### 2. 安装 dist 目录下的 tar.gz 源码包

若通过 `python setup.py sdist` 或 `python -m build` 生成了 `dist/*.tar.gz` 源码包，可使用 pip 安装：

```powershell
# 安装 dist 目录下的 tar.gz 包（替换为实际文件名）
pip install dist\aktools-0.0.91.tar.gz
```

或使用绝对路径：

```powershell
pip install e:\projects\aktools\dist\aktools-0.0.91.tar.gz
```

> **注意**：安装 tar.gz 时会临时解压并执行 `setup.py` 构建，需确保已安装项目依赖（`pip install -r requirements.txt`）。

### 3. 启动服务

安装完成后，使用以下任一方式启动 HTTP API 服务：

**方式一：通过模块运行（推荐）**

```powershell
python -m aktools
```

**方式二：指定端口与主机**

```powershell
python -m aktools --port 8080 --host 127.0.0.1
```

**方式三：启动后自动打开浏览器**

```powershell
python -m aktools --auto
```

### 4. 访问服务

服务默认监听 `http://127.0.0.1:8080/`，可访问：

- 主页：<http://127.0.0.1:8080/>
- API 文档：<http://127.0.0.1:8080/docs>
- 版本信息：<http://127.0.0.1:8080/version>
- 数据接口示例：<http://127.0.0.1:8080/api/public/stock_zh_a_hist?symbol=600000>

### 5. Windows 下的常见说明

| 说明 | 备注 |
|------|------|
| 使用 PowerShell 或 CMD | 上述命令在 PowerShell 和 CMD 中均可执行 |
| 端口占用 | 若 8080 被占用，可通过 `--port 端口号` 修改 |
| 防火墙 | 若需外网访问，将 `--host` 设为 `0.0.0.0`，并允许防火墙放行对应端口 |
| Investing 数据 | 需安装 `curl_cffi` 以绕过 Cloudflare；若仍出现 403，可设置环境变量 `INVESTING_IMPERSONATE=safari184` 或 `chrome131` 尝试不同指纹 |
