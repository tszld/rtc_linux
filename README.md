# RTC 客服调度原型

局域网优先的 RTC 呼叫原型，包含三个组件：

- `caller/`：呼叫电脑上的本机网页客户端。
- `agent/`：PySide6 客服端，内嵌 QtWebEngine 运行 DingRTC Web SDK。
- `server/`：FastAPI 集中后端，只处理登录、房间状态、原子接听和 RTC Token，不转发音视频。

## 环境

- Windows 10/11 x64。
- Python 3.10.x，当前验证版本为 3.10.22。
- 两台电脑位于同一局域网，均能访问 DingRTC 云端。
- 客服电脑和呼叫电脑各自使用默认麦克风与扬声器。

## 安装

在项目根目录运行：

```powershell
.\setup.ps1
```

默认使用 Miniforge 环境：

```text
D:\ProgramFiles\Miniforge3\envs\rtc\python.exe
```

如需使用其他解释器，先设置 `RTC_PYTHON`：

```powershell
$env:RTC_PYTHON = "D:\path\to\python.exe"
.\setup.ps1
```

`setup.ps1` 只会使用有效的 `RTC_PYTHON`，否则使用上面的项目指定环境。
如果两者都不存在，脚本会停止并提示先创建环境；不会创建或启用已弃用的 `.venv`。

当前验证过的精确依赖版本记录在 `requirements.lock.txt`。需要复现同一环境时运行：

```powershell
& "D:\ProgramFiles\Miniforge3\envs\rtc\python.exe" -m pip install -r .\requirements.lock.txt
```

在已初始化 Conda 的 PowerShell 中激活统一环境：

```powershell
conda activate rtc
python --version
```

应显示 Python 3.10.22。如果当前 PowerShell 尚未初始化 Conda，可以不激活，
直接使用项目指定解释器运行后面的 `main.py`。

## 统一入口

项目根目录的 `main.py` 提供三个运行模式。激活 `rtc` 环境后：

```powershell
# 集中后端
python -X utf8 main.py server

# PySide6 客服端
python -X utf8 main.py agent

# 本机呼叫页面
python -X utf8 main.py caller

# Ctrl+C 无效时，从另一个终端停止本机后端
python -X utf8 main.py server --stop
```

各模式还支持独立参数，例如：

```powershell
python -X utf8 main.py server --host 0.0.0.0 --port 8000
python -X utf8 main.py caller --backend http://100.64.0.2:8000
```

## 配置

集中后端需要 DingRTC AppKey。不要把 AppKey 写入客户端，也不要写入受版本控制的
`server/config.json`。后端脚本使用不进入版本控制的 `server/config.local.json`：

```json
{
  "app_id": "cn4zs9jm",
  "app_key": "your-app-key"
}
```

`server/config.local.json` 已由 `.gitignore` 排除。将 `your-app-key` 替换为真实
AppKey 后，直接运行后端，不需要设置环境变量：

```powershell
python -X utf8 main.py server
```

`server/config.json` 只保存可直接进入版本控制的默认配置，其中 `app_key` 必须保持为空。

电脑 B 启动后端：

```powershell
python -X utf8 main.py server
```

后端监听 `0.0.0.0:8000`。在电脑 B 上以管理员 PowerShell 执行以下命令，
仅允许专用网络访问该端口：

```powershell
New-NetFirewallRule -DisplayName "RTC Backend 8000" -Direction Inbound -Protocol TCP -LocalPort 8000 -Action Allow -Profile Private
```

不需要把该端口暴露到公网，也不要为当前原型开放公用网络配置文件。

## 两台电脑运行

电脑 B 修改 `agent/config.json`：

```json
{
  "backend_url": "http://127.0.0.1:8000"
}
```

然后运行：

```powershell
python -X utf8 main.py agent
```

电脑 A 修改 `caller/config.json`，将 `backend_url` 改为电脑 B 的局域网地址，例如：

```json
{
  "backend_url": "http://192.168.1.20:8000",
  "client_id": "caller-pc-a",
  "default_name": "呼叫客户端 A"
}
```

然后运行：

```powershell
python -X utf8 main.py caller
```

呼叫页面必须从电脑 A 的 `127.0.0.1` 加载，才能可靠申请麦克风。页面再连接电脑 B 的集中后端。

两台电脑按以下顺序启动：

1. 电脑 B 启动后端，并确认 `http://127.0.0.1:8000/api/health` 可访问。
2. 电脑 B 启动客服端，分别登录需要使用的账号。
3. 电脑 A 启动呼叫页面，再发起呼叫。

## 开发账号

首次启动后端时会在 SQLite 中创建两个开发账号：

- `agent01` / `agent123`
- `agent02` / `agent123`

账密只适合局域网原型。公网部署前必须修改预置账号和密码，并启用 HTTPS/WSS。

## 验证流程

1. 电脑 B 启动后端和两个 PySide6 客服实例，分别登录两个账号。
2. 电脑 A 启动呼叫页面并点击“发起呼叫”。
3. 两个客服端应同时看到 `waiting` 房间。
4. 在一个客服端点击“接听”，另一个客服端应在 1 秒内显示“连接中”或“通话中”。
5. 双方检查默认麦克风和扬声器能否双向听到声音。
6. 呼叫端点击“共享屏幕”，客服端应看到呼叫端选择的屏幕画面；停止共享后画面应消失。
7. 任意一方主动挂断后，呼叫页面和客服端都应结束当前通话并退出 RTC。

## 自动测试

```powershell
& "D:\ProgramFiles\Miniforge3\envs\rtc\python.exe" -m unittest discover -s tests -v
```

自动测试不访问 DingRTC 云端。真实双向音频和屏幕共享必须在两台电脑上人工验收。

当前自动化结果：

- 完整自动化测试 `30/30` 通过。
- Python 代码通过 `compileall` 语法检查。
- 呼叫端和客服端 JavaScript 通过 `node --check` 语法检查。
- 真实桌面麦克风权限、真实 DingRTC 入会和双向音频尚未在当前环境验收。

## 安全边界

当前版本仅用于可信局域网原型，不支持公网部署。公网使用前必须完成：

- 使用 HTTPS 和 WSS，并配置域名与有效证书。
- 修改开发账号、密码和密钥管理方式，增加登录保护与限流。
- 限制后端监听范围、防火墙来源和反向代理配置。
- 对日志、数据库、配置和备份执行秘密信息审计。

## 当前边界

当前实现双向音频、呼叫端向客服端共享屏幕、房间列表、占用同步、客服登录、接听冲突和挂断。未实现摄像头视频、真实机器人、Electron、控制流、公网 TLS 部署、媒体录制和后台管理。
