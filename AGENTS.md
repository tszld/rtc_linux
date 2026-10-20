# RTC 项目协作规则

本文件是本项目的窗口级接手入口。开始工作前先读本文件，再按当前任务读取
`TASKS.md`、`README.md` 和相关源码。`TASKS.md` 保留了迁移前的原始计划，其中
旧的路径和 `.venv` 命令属于历史内容；发生冲突时，以本文件、当前代码和实际验证
结果为准。

## 1. 项目目标与范围

这是一个局域网优先的 RTC 呼叫原型，由三部分组成：

- `caller/`：呼叫端本机网页，负责发起呼叫、发布麦克风、共享屏幕、显示会话状态和挂断。
- `agent/`：PySide6 客服与管理员端，负责客服登录、客户端目录、接听、管理账号和客户端。
- `server/`：FastAPI 集中控制面，负责客服/管理员账号、客户端注册审批、会话状态、
  DingRTC Token 和 WebSocket 推送。

当前范围包含双向音频、呼叫端到客服端的单向屏幕共享、客户端注册审批和轻量管理后台。
不包含摄像头视频、在线心跳、控制流、真实机器人、Electron、公网 TLS 和媒体录制。

固定架构约束：

- 后端只做控制面，不转发音频或视频。
- 呼叫端和客服端直接加入同一个 DingRTC 房间。
- 所有客服共享同一个后端和同一份 SQLite 会话状态。
- 当前是单进程 SQLite 原型，不为本阶段引入 PostgreSQL、Redis、消息队列、
  Docker 或多 Uvicorn worker。

## 2. 运行环境

项目要求 Python 3.10.x，当前验证版本为 3.10.22。优先激活已经安装项目依赖的
Python 环境；也可以使用相对路径指定解释器：

```powershell
$env:RTC_PYTHON = ".\runtime\python.exe"
```

相对路径以项目根目录为基准。根目录 `.venv/` 是已弃用的历史环境，不要继续使用，
也不要在未获明确要求时删除。

安装或更新已验证依赖：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\setup.ps1
```

标准自动化测试：

```powershell
python -m unittest discover -s tests -v
```

Python 语法检查：

```powershell
python -m compileall -q server agent caller tests
```

前端脚本语法检查：

```powershell
node --check .\caller\web\app.js
node --check .\agent\rtc\rtc.js
node --check .\agent\rtc\qwebchannel.js
```

启动入口：

```powershell
python -X utf8 main.py server
python -X utf8 main.py agent
python -X utf8 main.py caller
```

后端默认监听 `0.0.0.0:8000`。呼叫页面固定从本机
`http://127.0.0.1:8765` 加载，才能可靠保存 Origin、浏览器权限和客户端身份；
页面再访问电脑 B 的集中后端。

## 3. 模块职责与修改边界

### `server/`

- `app.py`：HTTP/WebSocket 路由和应用生命周期。
- `auth.py`：客服登录和访问令牌。
- `admin.py`：超级管理员、客服账号管理和管理员审计。
- `clients.py`：客户端注册、审批状态、设备凭证和客服客户端目录。
- `calls.py`：会话创建、认领、状态流转、断线、恢复和超时。
- `db.py`：SQLite 初始化和事务辅助。
- `config.py`：配置加载和环境变量覆盖。
- `rtc.py`：DingRTC Token 生成。
- `ws_manager.py`：WebSocket 连接、票据和状态广播。
- `token/`：DingRTC Token 生成依赖，视为外部算法实现，不做无关改动。

修改后端行为时必须优先保持状态机和鉴权语义，不以界面现象代替事务正确性。

### `agent/`

- `main.py`：PySide6 客服/管理员登录、客户端目录、会话列表、管理页面和 WebSocket。
- `api.py`：客服端 HTTP 客户端。
- `rtc_panel.py`：QtWebEngine RTC 页面宿主和权限处理。
- `rtc/`：客服端 RTC 页面、WebChannel 桥接和麦克风控制。

不要在客服端保存或打印 AppKey，也不要绕过后端的会话认领接口直接加入房间。

### `caller/`

- `launcher.py`：固定端口本机静态页面、Installation UUID 和设备凭证持久化。
- `web/`：客户端注册审批、状态展示、DingRTC 音频与屏幕发布和 WebSocket 重连。

呼叫页面不能直接请求局域网 HTTP RTC 能力；麦克风入口必须是本机
`127.0.0.1`/`localhost` 页面。修改呼叫流程时不得改变后端公共接口。

### `tests/`

- 自动化测试不访问 DingRTC 云服务。
- 后端状态机、鉴权、并发、超时和 WebSocket 行为应有可重复测试。
- 真实音频、屏幕共享、真实设备和真实网络问题属于人工验收，不能由单元测试替代。

## 4. 会话状态机与安全约束

核心状态为：

```text
created -> waiting -> connecting -> active -> ended
                         |
                         +-> waiting

connecting/active -> interrupted -> connecting/active/waiting/ended
```

必须保持以下不变量：

- 只有 `waiting` 可以通过原子操作变为 `connecting`。
- 一个客服不能并发登录，也不能同时占用多个会话。
- 接听前不加入 RTC，不申请或发布客服麦克风。
- 任意一方主动挂断后，会话立即进入 `ended`，双方退出 RTC。
- 呼叫客户端连接断开后立即结束会话，不进入 caller `interrupted`。
- 客服异常断线进入 `interrupted`，默认保留 15 秒；超时结束会话。
- 客服重启后可认领并恢复自己的 `interrupted` 会话。
- 客户端创建呼叫必须同时校验 Installation UUID 和设备凭证。
- 呼叫端结束和查询自己的会话必须使用本次 caller token 校验。
- WebSocket 票据是一次性的，并有过期时间。
- 后端重启时，所有未结束会话必须被明确收尾，不能遗留假活动状态。
- 数据库只保存 token 哈希，不保存明文密码或明文 caller token。

安全检查：

- DingRTC AppKey 只能来自后端环境变量 `RTC_APP_KEY` 或不进版本控制的
  `server/config.local.json`。
- AppKey 不得进入网页、PySide6 配置、日志、响应、测试输出或提交内容。
- 不支持公网部署。公网使用前必须增加 HTTPS/WSS、域名、证书、账号安全和限流。
- 不硬编码其他密码、Token、API Key 或敏感信息。

## 5. 配置与网络

- 后端默认配置：`server/config.json`。
- 后端本地秘密：`server/config.local.json`，必须保持被 git 忽略。
- 客服端配置：`agent/config.json`。
- 呼叫端配置：`caller/config.json`。
- 客户端身份和设备凭证：`caller/config.local.json`，必须保持被 git 忽略。
- 开发账号目前为 `agent01/agent123` 和 `agent02/agent123`，仅适合局域网原型。
- 超级管理员不预置，使用 `main.py server --create-admin` 创建。
- 客服电脑需允许同一局域网访问 TCP `8000`；不要扩大到不必要的来源。
- 电脑 A 和电脑 B 都必须能够访问 DingRTC 云服务。

## 6. 当前阶段状态

最近更新日期：2026-10-20。

- `T0`：已完成，源码已迁移到 `D:\Projects\rtc`。
- `T1`：已完成。后端事务、并发接听、超时、WebSocket 票据、断线恢复和重启收尾
  已加固；当前完整自动化测试达到 `51/51` 通过。
- `T2`：自动化和模拟验证已完成主要部分，包括 WebChannel 事件桥接、双客服同步、
  同账号重复登录拒绝、入会中挂断清理和重启恢复；真实桌面麦克风权限、真实
  DingRTC 入会和真实音频尚未验收。
- `T3`：按用户要求暂停。新窗口不得自动继续 T3，必须等用户明确恢复。
- `T4`：环境阻塞。当前未配置真实 AppKey，也没有两台电脑、真实麦克风和真实
  双向 DingRTC 条件，不能宣称端到端音频通过。
- `T5`：部分完成。启动脚本和 README 已切换到指定环境；`requirements.lock.txt`、
  完整秘密扫描、忽略规则复核和最终交付检查仍未完成。

`TASKS.md` 中的历史状态不代表当前完成度；每次汇报必须区分“代码已修改”、
“自动化已验证”和“真实设备已验收”。

## 7. 新窗口接手规则

1. 先读本文件、`README.md`、`TASKS.md` 和当前任务相关源码。
2. 先检查工作树状态，保留用户已有修改，不覆盖、不回滚无关文件。
3. 确认当前只执行用户明确指定的任务。T3 当前暂停，不得顺手启动或修改。
4. 修改前说明目标、修改边界和验证方式；小范围、低风险实现自行完成。
5. 用户未明确要求时，不提交、不推送、不删除文件、不新增重要依赖、不修改公共
   接口、核心数据模型或模块边界。
6. 完成后必须给出修改文件、实际执行的命令和验证结果。无法执行的真实设备或
   网络验收要明确列为阻塞，不能用模拟结果替代。
7. 当前任务验证完成后停止，不主动扩大范围。
