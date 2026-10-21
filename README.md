# RTC 客服调度原型

局域网优先的 RTC 呼叫原型，包含三个组件：

- `caller/`：呼叫电脑上的固定端口本机网页客户端，自动注册并等待管理员审批。
- `agent/`：PySide6 客服与管理员端，内嵌 QtWebEngine 运行 DingRTC Web SDK。
- `server/`：FastAPI 集中后端，处理账号、客户端审批、房间状态、原子接听和 RTC Token，不转发音视频。

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

推荐先激活 Python 3.10 环境。也可以使用相对路径或环境变量指定解释器：

```powershell
$env:RTC_PYTHON = ".\runtime\python.exe"
.\setup.ps1
```

`setup.ps1` 优先使用 `RTC_PYTHON`，否则使用 PATH 中的 `python` 或 Windows
`py -3.10`。相对路径以项目根目录为基准。不会创建或启用已弃用的 `.venv`。

依赖清单按运行形态拆分：

| 文件 | 用途 |
|---|---|
| `requirements.txt` | Windows 全量开发环境 |
| `requirements-server.txt` | 只运行 FastAPI 控制端，适合 Linux 云服务器 |
| `requirements-caller.txt` | 呼叫端无 pip 依赖，仅作明确说明 |
| `requirements-agent.txt` | PySide6 Windows 客服端 |

安装完整环境或指定组件：

```powershell
.\setup.ps1
.\setup.ps1 -Component server
.\setup.ps1 -Component caller
.\setup.ps1 -Component agent
```

Linux 云服务器只安装服务端依赖：

```bash
python3.10 -m pip install -r requirements-server.txt
python -X utf8 main.py server
```

呼叫端启动器只使用 Python 标准库；DingRTC 通过静态 `web/sdk.js` 加载，不需要
pip 包。JavaScript 自动化测试需要 Node.js，但 Node.js 不是三种运行端的运行依赖。

当前验证过的精确 Windows 全量依赖记录在 `requirements.lock.txt`。需要复现同一环境时运行：

```powershell
python -m pip install -r .\requirements.lock.txt
```

该锁文件包含 PySide6，只适用于当前 Windows 开发环境，不能直接作为 Linux 服务端锁文件。

在已初始化 Conda 的 PowerShell 中激活统一环境：

```powershell
conda activate rtc
python --version
```

应显示 Python 3.10.22。如果当前 PowerShell 尚未初始化 Conda，可以设置
`RTC_PYTHON` 或直接使用 PATH 中的 `python`。

## 统一入口

项目根目录的 `main.py` 提供三个运行模式。激活 `rtc` 环境后：

```powershell
# 集中后端
python -X utf8 main.py server

# PySide6 客服端
python -X utf8 main.py agent

# 本机呼叫页面
python -X utf8 main.py caller

# 首次部署，交互式创建唯一超级管理员
python -X utf8 main.py server --create-admin

# 超级管理员忘记密码时交互式重置
python -X utf8 main.py server --reset-admin-password

# Ctrl+C 无效时，从另一个终端停止本机后端
python -X utf8 main.py server --stop
```

只有 `--create-admin` 和 `--reset-admin-password` 会要求交互输入管理员密码。
普通 `python -X utf8 main.py server` 启动不会创建管理员，也不会提示密码。

各模式还支持独立参数，例如：

```powershell
python -X utf8 main.py server --host 0.0.0.0 --port 8000
python -X utf8 main.py caller --backend http://100.64.0.2:8000
python -X utf8 main.py caller --port 8765
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

默认数据库是相对路径 `server/data/rtc.db`。配置中的 `db_path` 或环境变量
`RTC_DB_PATH` 如果使用相对路径，统一相对于项目根目录解析，不受启动时当前目录影响。

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
  "default_name": "呼叫客户端 A",
  "port": 8765
}
```

`installation_uuid` 和审批后的设备凭证由启动器自动写入不进入版本控制的
`caller/config.local.json`。不要在配置中手工填写可冒充其他客户端的编号。
`caller/config.json` 的 `default_name` 非空时作为固定客户端名称；为空时会自动
生成 `CALL-XXXXXXXX` 并保存到 `config.local.json`，后续启动保持不变。呼叫页面
只展示客户端名称，超级管理员可以在客户端管理页面改名。
客户端被拒绝或停用后，页面会显示“重新注册”，原客户端记录重新进入待审批，
管理员仍需再次批准。

如果页面提示“客户端设备凭证无效”，说明本机凭证与后端保存的凭证哈希已经不匹配。
此时“重新注册”会生成新的 `installation_uuid`、清除本机旧凭证并提交一个新的待审批
客户端记录。若希望沿用原客户端编号，应由管理员在“客户端审批”中点击“重置凭证”，
然后刷新呼叫页面。不同呼叫设备不能共用同一份 `config.local.json`。
两台客户端必须使用不同的 `installation_uuid`；否则并发呼叫会复用同一个会话，
导致两个呼叫端进入同一 RTC 房间并出现异常。

然后运行：

```powershell
python -X utf8 main.py caller
```

呼叫页面固定从电脑 A 的 `127.0.0.1:8765` 加载，才能可靠保存 Origin、浏览器权限
和客户端身份。页面再连接电脑 B 的集中后端。

两台电脑按以下顺序启动：

1. 电脑 B 首次部署时执行 `server --create-admin`，再启动后端。
2. 电脑 B 启动客服端，用超级管理员登录并审批客户端。
3. 电脑 B 用客服账号登录，查看空闲和占用客户端。
4. 电脑 A 启动呼叫页面，等待管理员批准后发起呼叫。

## 开发账号

首次启动后端时会在 SQLite 中创建两个开发账号：

- `agent01` / `agent123`
- `agent02` / `agent123`

账密只适合局域网原型。公网部署前必须修改预置账号和密码，并启用 HTTPS/WSS。
超级管理员不会预置，必须通过服务器命令创建。

## 验证流程

1. 超级管理员登录管理界面，批准待审批客户端。
2. 电脑 B 启动两个客服实例，分别登录两个客服账号。
3. 两个客服端应在客户端目录看到同一批已批准客户端及空闲/占用状态。
4. 电脑 A 点击“发起呼叫”；空闲客服端应出现新呼叫提示、提示音和“接听”按钮，
   呼叫端在客服点击接听前保持“排队中”。
5. 两个客服都在通话时，再发起呼叫，呼叫端应显示“排队中”和当前排队位置。
6. 任一客服挂断后，队列首位呼叫应提示空闲客服，由客服点击“接听”后进入 RTC。
7. 双方检查默认麦克风和扬声器能否双向听到声音。
8. 呼叫端点击“共享屏幕”，客服端应看到呼叫端选择的屏幕画面。
9. 任意一方主动挂断后，呼叫页面和客服端都应结束当前通话并退出 RTC。
10. 管理员停用客服或客户端后，其旧登录和旧设备凭证应立即失效。

## 自动测试

```powershell
python -m unittest discover -s tests -v
```

自动测试不访问 DingRTC 云端。真实双向音频和屏幕共享必须在两台电脑上人工验收。

当前自动化结果：

- 完整自动化测试 `50/50` 通过。
- Python 代码通过 `compileall` 语法检查。
- 呼叫端和客服端 JavaScript 通过 `node --check` 语法检查。
- 真实桌面麦克风权限、真实 DingRTC 入会和双向音频尚未在当前环境验收。

## 安全边界

当前版本仅用于可信局域网原型，不支持公网部署。公网使用前必须完成：

- 使用 HTTPS 和 WSS，并配置域名与有效证书。
- 修改开发账号、密码和密钥管理方式，增加登录保护与限流。
- 停用管理员、客服或客户端时必须吊销现有凭证。
- 限制后端监听范围、防火墙来源和反向代理配置。
- 对日志、数据库、配置和备份执行秘密信息审计。

## 当前边界

当前实现双向音频、呼叫端向客服端共享屏幕、客户端注册审批、超级管理员、客服账号管理、客户端目录、占用同步、接听冲突和挂断。未实现摄像头视频、在线心跳、真实机器人、Electron、控制流、公网 TLS 部署、媒体录制和复杂后台权限。
