# RTC 客服调度原型任务书

更新日期：2026-10-19

## 1. 项目目标

完成一个两台电脑局域网可运行的原型：

- 电脑 A：呼叫客户端，本机网页发布麦克风音频。
- 电脑 B：PySide6 客服端，登录集中后端，查看房间列表，接听并双向通话。
- 电脑 B：独立集中后端，负责账号、房间状态、原子接听、RTC Token 和 WebSocket 推送。
- DingRTC 只负责音频房间和媒体传输。

当前只实现双向音频。屏幕共享、控制流、真实机器人、Electron、公网 TLS 和媒体录制不在本轮范围。

## 2. 路径和现状

当前可运行源码暂存目录：

```text
C:\Users\user\.codex\visualizations\2026\10\19\01a15304-e246-70f2-8286-173fc75812f9\rtc
```

最终目标目录：

```text
D:\Projects\rtc
```

当前目标目录可能为空。不要从零重写，先迁入暂存目录中的现有实现。

已完成的代码：

- `server/`：FastAPI 后端、SQLite、客服登录、原子接听、WebSocket、RTC Token。
- `agent/`：PySide6 登录页、房间列表、接听、QWebEngineView RTC 页面、静音和挂断。
- `caller/`：本机 HTTP 页面、呼叫创建、麦克风发布、静音和挂断。
- `tests/test_backend.py`：后端服务测试和 HTTP 冲突测试。
- `README.md`：安装、配置和两台电脑运行说明。

已通过的验证：

- Python `compileall` 通过。
- 呼叫端和客服端 JavaScript `node --check` 通过。
- 后端 3 个自动测试通过。
- PySide6 主窗口可在 offscreen 模式实例化。
- PySide6、QtWebEngine、QtWebSockets 可以导入。

尚未通过的验证：

- 真实桌面模式下 QtWebEngine 本地 RTC 页面加载尚未完成验收。
- 没有执行真实 DingRTC 双向音频测试。
- 没有在两台真实电脑和两个客服实例上完成端到端验收。
- 运行过程中曾生成 `server/token/util/util/` 和历史 `__pycache__/`，迁移时应排除。

## 3. 不可改变的架构约束

- 集中后端不转发音频，不处理屏幕。
- 呼叫端和客服端直接连接同一个 DingRTC 房间。
- 所有客服共享同一个后端和同一份房间状态。
- 一个客服账号不能并发登录。
- 一个客服同时只占用一条会话。
- 只有 `waiting` 可以原子变为 `connecting`。
- 接听前不加入 RTC，不试听。
- 任一方主动挂断后会话立即结束，另一端同步退出 RTC。
- 客服异常断开保留 15 秒，超时结束会话。
- AppKey 只能存在后端环境变量或 `server/config.local.json`，不能进入网页、PySide6 配置或日志。
- 本轮不要引入 PostgreSQL、Redis、消息队列、Docker 或跨后端集群。

## 4. 任务总览

| 任务 | 内容 | 依赖 | 可并行 |
|---|---|---|---|
| T0 | 迁移项目到 `D:\Projects\rtc` | 无 | 必须最先做 |
| T1 | 后端和状态机加固 | T0 | 可与 T2、T3 并行 |
| T2 | PySide6 客服端验收和修复 | T0 | 可与 T1、T3 并行 |
| T3 | 呼叫网页验收和修复 | T0 | 可与 T1、T2 并行 |
| T4 | 两台电脑端到端音频验收 | T1、T2、T3 | 最后执行 |
| T5 | 安全、配置和交付整理 | T1、T2、T3 | 可与 T4 交错 |

文件所有权：

- T1 主要修改 `server/`、`tests/`。
- T2 主要修改 `agent/`。
- T3 主要修改 `caller/`。
- T5 可以修改 `README.md`、`setup.ps1`、`requirements.txt` 和配置模板。
- T4 原则上只记录问题，不在验收过程中同时大改代码。

## 5. T0：迁移和初始化

负责人：第一个执行窗口。

输入：

- 暂存源码目录存在。
- `D:\Projects\rtc` 已存在或可以创建。

执行步骤：

1. 检查 `D:\Projects\rtc` 是否为空。
2. 如果目录非空，先列出内容并停止覆盖，等待人工确认。
3. 从暂存目录复制到目标目录。
4. 排除 `.venv/`、`__pycache__/`、`*.pyc`、`server/data/`、`server/token/util/util/`。
5. 在目标目录重新创建虚拟环境并安装依赖。
6. 运行 `python -m compileall -q server agent caller tests`。
7. 运行 JavaScript 语法检查。

参考复制命令：

```powershell
$source = "C:\Users\user\.codex\visualizations\2026\10\19\01a15304-e246-70f2-8286-173fc75812f9\rtc"
$target = "D:\Projects\rtc"
robocopy $source $target /E /XD ".venv" "__pycache__" "data" "util\util" /XF "*.pyc"
```

验收标准：

- `D:\Projects\rtc\README.md` 存在。
- `D:\Projects\rtc\.venv\Scripts\python.exe` 存在。
- 暂存目录和目标目录之外的旧项目文件没有被修改。
- 没有复制 AppKey、SQLite 数据库和 `__pycache__`。

## 6. T1：后端和状态机加固

负责人：后端窗口。

允许修改：

- `server/**`
- `tests/**`

不要修改：

- `agent/**`
- `caller/**`
- 当前架构约束。

执行步骤：

1. 运行现有测试：

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

2. 审核 `server/auth.py` 的重复登录检查，使用 SQLite `BEGIN IMMEDIATE` 消除两个并发登录同时通过的窗口。
3. 增加以下测试：

- 未登录访问 `/api/calls` 返回 401。
- 错误呼叫方 Token 返回 403。
- 两个客服并发接听，只有一个成功。
- 任一方主动挂断后状态变为 `ended`。
- `connecting` 超时回到 `waiting`。
- `interrupted` 超时变为 `ended`。
- 同一个客服不能同时占用两条会话。
- 客服登出后可以重新登录。
- WebSocket 第一次连接收到 `snapshot`。

4. 检查 `server/app.py` 中 WebSocket 票据只能使用一次。
5. 检查客服 WebSocket 断开后，已接线进入 `interrupted`。
6. 检查后端重启时所有未结束会话被标记为 `ended/backend_restart`。
7. 确认日志、响应和数据库中不出现 AppKey 或明文密码。

验收标准：

- 所有自动测试通过。
- 并发接听测试至少使用 10 个并发请求。
- 不存在两个客服同时得到接听成功的情况。
- WebSocket 断开、重连和超时行为有测试或可重复验证记录。

## 7. T2：PySide6 客服端验收和修复

负责人：客服端窗口。

允许修改：

- `agent/**`

不要修改：

- 后端接口字段，除非先在任务记录中注明。
- 呼叫端页面。

执行步骤：

1. 启动后端，使用 `agent01` 和 `agent02` 启动两个 `agent.main` 实例。
2. 登录后确认列表通过 WebSocket 自动更新。
3. 确认一个客服接听后，另一个客服在一秒内看到“连接中”或“通话中”。
4. 在真实桌面模式下打开 QWebEngineView，确认本地 RTC 页面加载成功。
5. 确认麦克风权限请求被授予，控制台没有 `qwebchannel`、`sdk.js` 或权限错误。
6. 确认 QWebChannel 可以双向传递 `connecting`、`joined`、`error`、`stopped`。
7. 接入真实 AppKey 后确认客服端能加入房间、发布麦克风、订阅呼叫端音频。
8. 测试远端声音自动播放失败时，“播放远端声音”按钮可以恢复播放。
9. 测试主动挂断、呼叫端结束、WebSocket 断开重连和 15 秒异常超时。
10. 测试同账号第二个窗口登录被拒绝。

重点检查：

- `agent/rtc_panel.py` 的桌面权限回调。
- `agent/rtc/rtc.js` 的清理和重复加入逻辑。
- `agent/main.py` 在客服应用重启后恢复 `interrupted` 会话的逻辑。
- 当前实现尚未把重启后发现的自有中断会话自动设为 `current_call`，应补齐。

验收标准：

- 两个客服窗口能稳定运行至少 10 分钟。
- 页面加载、登录、接听、静音、挂断均有明确界面状态。
- 不出现重复 RTC 客户端、重复订阅或挂断后仍播放远端声音。

## 8. T3：呼叫网页验收和修复

负责人：呼叫端窗口。

允许修改：

- `caller/**`

不要修改：

- `agent/**`
- 后端状态机。

执行步骤：

1. 在电脑 A 运行 `start_caller.cmd`，确认页面地址为 `http://127.0.0.1:<随机端口>`。
2. 将 `caller/config.json` 的 `backend_url` 改为电脑 B 的局域网地址。
3. 确认页面可以访问 `/api/health`。
4. 点击“发起呼叫”，确认麦克风授权和 RTC 发布成功。
5. 确认状态依次显示 `created`、`waiting`、`connecting`、`active`。
6. 确认客服挂断后页面收到 `ended` 并停止麦克风与 RTC。
7. 确认点击挂断后后端会话立即结束。
8. 关闭页面后确认会话通过 `pagehide` 结束。
9. 人为关闭后端 WebSocket，确认页面重连或明确显示连接异常。
10. 同时启动两个不同 `client_id` 的呼叫实例，确认出现两个独立房间。

验收标准：

- 浏览器不出现 CORS 错误。
- 不使用局域网 HTTP 页面直接申请麦克风。
- 页面不会在 `call.status === "ended"` 后继续发布音频。
- 任何异常都能在页面看到明确中文错误。

## 9. T4：两台电脑端到端验收

负责人：集成窗口。

前置条件：

- T0、T1、T2、T3 完成。
- 有有效 DingRTC AppID 和 AppKey。
- 电脑 A 和电脑 B 在同一局域网。
- 电脑 B 防火墙允许专用网络访问 8000 端口。
- 两台电脑都有可用麦克风和扬声器。

执行步骤：

1. 电脑 B 设置 `RTC_APP_ID` 和 `RTC_APP_KEY`，启动集中后端。
2. 电脑 B 启动两个 PySide6 客服实例，分别登录 `agent01` 和 `agent02`。
3. 电脑 A 使用电脑 B 的局域网 IP 启动呼叫页面。
4. 发起一路呼叫，确认两个客服列表同时出现该房间。
5. 客服需要点击“接听”，不要跳过 RTC 连接后的状态确认。
6. 客服 `agent01` 接听，确认 `agent02` 立即看到已占用。
7. 双方用默认麦克风和扬声器实际说话，记录能否双向听到。
8. 客服主动挂断，确认客服端与呼叫端都结束会话并退出 RTC。
9. 呼叫端重新发起新会话，确认 `agent02` 可以接听并建立音频。
10. 关闭客服窗口模拟异常，记录 15 秒后的会话结果。
11. 关闭呼叫窗口，确认客服端不再处于活动通话。

验收标准：

- 真实双向音频成功。
- 同一房间不会出现两个客服同时通话。
- 客服窗口关闭后前端明确清理音频。
- 呼叫端断开后客服端不会继续显示假的活动状态。
- 失败场景记录包括时间、电脑、账号、房间编号、错误截图和日志。

## 10. T5：配置、安全和交付整理

负责人：交付窗口。

允许修改：

- `README.md`
- `setup.ps1`
- `requirements.txt`
- 配置模板和启动脚本。

执行步骤：

1. 让 `setup.ps1` 不再硬编码当前机器上的 Codex Python 路径。
2. 安装逻辑优先使用 `py -3.12`，其次使用 `python`，最后才使用本机显式配置路径。
3. 创建 `requirements.lock.txt`，记录实际验证过的依赖版本。
4. 检查所有配置文件没有真实 AppKey。
5. 将开发账号密码在 README 中标记为仅局域网原型。
6. 检查 `.gitignore` 覆盖 `.venv`、数据库、日志、`config.local.json` 和 `__pycache__`。
7. 清理迁移目标中不应存在的 `server/token/util/util/` 和 `__pycache__/`。
8. 补充 Windows 防火墙配置和两台电脑启动顺序。
9. 在 README 中写明当前不支持公网，公网前必须增加 HTTPS/WSS、域名、证书和账号安全策略。

验收标准：

- 新电脑按照 README 可以完成安装和启动。
- 源码、配置、日志和数据库中都没有 AppKey。
- 依赖版本可以复现。

## 11. 已知风险

- QtWebEngine 的麦克风权限、自动播放和显卡兼容性必须在真实桌面验证。
- DingRTC 入会成功不等于能听到声音，必须以真实双向音频为验收依据。
- 同一台电脑启动两个客服实例测试占用时可能产生回声，建议使用耳机或静音其中一路。
- 当前后端是单进程 SQLite，不支持多 Uvicorn worker。
- 当前没有公网部署、TLS、限流和账号管理。
- 当前没有屏幕共享和控制流。

## 12. 给其他窗口的启动提示

### 后端窗口

```text
读取 D:\Projects\rtc\TASKS.md，只执行 T1。不要修改 agent/ 和 caller/。先运行现有测试，再补并发、超时和 WebSocket 测试。完成后汇报修改文件、测试命令和结果。
```

### 客服端窗口

```text
读取 D:\Projects\rtc\TASKS.md，只执行 T2。重点验证 PySide6、QtWebEngine、QWebChannel、麦克风权限和真实 RTC 入会。不要修改 server/ 和 caller/。完成后汇报验证设备和音频结果。
```

### 呼叫端窗口

```text
读取 D:\Projects\rtc\TASKS.md，只执行 T3。重点验证本机 HTTP 页面、电脑 A 到电脑 B 的后端访问、麦克风发布、WebSocket 重连和挂断。不要修改 server/ 和 agent/。
```

### 集成窗口

```text
读取 D:\Projects\rtc\TASKS.md，执行 T4。使用两台电脑、两个客服实例和真实 DingRTC AppKey，记录每个失败场景的截图、日志和复现步骤。不要在大范围改代码的同时进行验收。
```

## 13. 当前交付状态

更新日期：2026-10-19。

- T3 仍按用户要求暂停，本轮未修改 `caller/`。
- T4 仍受真实 AppKey、两台电脑、真实麦克风和双向 DingRTC 音频条件阻塞。
- T5 的本地整理已完成：指定 Python 环境安装逻辑、精确依赖锁、配置秘密检查、
  `.gitignore` 复核、生成缓存清理、README 安全与启动说明已处理。
- T5 的“另一台新电脑按 README 完成安装和启动”仍需在真实新环境人工验收。
