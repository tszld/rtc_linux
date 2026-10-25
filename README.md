# ARTC 业务语音机器人与客服协作平台

项目由 Linux 机器人端、Linux 业务控制端和浏览器客服工作台组成。机器人媒体使用
阿里云 ARTC Linux Python SDK，客服媒体使用阿里云 ARTC Web SDK，业务后端不转发或
保存音视频。

## 架构

```text
robot/       Linux ARTC Python SDK 机器人
server/      FastAPI + SQLite 业务控制面
agent/web/   ARTC Web SDK 浏览器客服工作台
agent/web_host.py
             独立客服 Web 静态服务
agent/web/vendor/aliyun-rtc-sdk.js
             阿里云 ARTC Web SDK 7.3.4
tools/       Token、机器人联调和秘密扫描工具
```

业务后端只负责身份、场景权限、机器人目录、在线状态、业务会话、人工接管租约、短期
ARTC Token、WebSocket 状态和审计，不托管客服页面。客服 Web 页面和 ARTC Web SDK
由独立的 `agent` 进程提供，浏览器再跨源访问控制端。

## 首次部署：按顺序完成三个端

首次部署不要同时启动三个端。按下面的顺序操作，前一步验证通过后再做下一步。

| 顺序 | 主机 | 组件 | 作用 |
|---|---|---|---|
| 1 | Linux 控制端 | `server` | 共享业务后端、SQLite、Token 和 WebSocket |
| 2 | Windows 客服电脑 | `agent` | 客服页面和管理员页面 |
| 3 | 各 Linux 机器人主机 | `robot` | ARTC 媒体、麦克风和机器人扬声器 |

控制端、客服电脑和每台机器人主机都必须能访问控制端的 `cluster.control_url`。使用
Tailscale 时，这些主机都要登录同一个 tailnet。

端口只承担控制面：

```text
控制端       监听 0.0.0.0:8000（通过 Tailscale-IP:8000 访问）
Windows 客服  仅监听 127.0.0.1:8100
机器人端      不监听业务控制端口
ARTC 媒体     机器人/客服直接连接阿里云，不经过控制端
```

### 第 1 步：部署 Linux 控制端

进入控制端项目目录，在已有的 `rtc` Python 环境中操作：

```bash
conda activate rtc
python --version
python -m pip install -r requirements-server.txt
```

在项目根目录创建 `config.json`。控制端只配置 `server` 节，`AppID`、`AppKey` 和
数据库只放在控制端：

```json
{
  "server": {
    "host": "0.0.0.0",
    "port": 8000,
    "app_id": "<ARTC AppID>",
    "app_key": "<ARTC AppKey>",
    "db_path": "server/data/artc.db",
    "cors_origins": ["http://127.0.0.1:8100"]
  }
}
```

首次创建数据库和管理员：

```bash
chmod 600 config.json
python main.py server --create-admin admin
```

该命令会提示两次输入管理员密码，密码不会写入配置文件。随后启动控制端：

```bash
python main.py server
```

另开终端验证：

```bash
curl http://<控制端Tailscale-IP>:8000/api/health
```

看到 `{"ok":true,...}` 后再继续。

### 第 2 步：启动 Windows 客服和管理员页面

在 Windows 项目根目录创建 `config.json`，只配置共享控制地址和 `agent` 节。这里不
需要 AppKey、数据库和机器人凭证：

```json
{
  "cluster": {
    "control_url": "http://<控制端Tailscale-IP>:8000"
  },
  "agent": {
    "host": "127.0.0.1",
    "port": 8100,
    "auto_open_browser": true
  }
}
```

启动客服 Web 服务：

```powershell
python main.py agent
```

先打开管理员页面：

```text
http://127.0.0.1:8100/admin.html
```

使用第 1 步创建的管理员账号登录，然后完成两件事：

1. 创建至少一个客服账号。
2. 创建机器人条目，并保存页面只显示一次的 `Robot ID` 和 `Credential`。

此时还没有启动机器人，所以机器人列表通常显示离线或待审批。

### 第 3 步：部署 Linux 机器人端

在机器人项目根目录创建 `config.json`，只配置共享控制地址和 `robot` 节：

```json
{
  "cluster": {
    "control_url": "http://<控制端Tailscale-IP>:8000"
  },
  "robot": {
    "robot_id": "<管理员页面生成的 Robot ID>",
    "credential": "<管理员页面生成的 Credential>",
    "display_name": "<机器人名称>",
    "sdk_root": "robot/vendor/AliRTCSDK_Linux-7.11.0",
    "log_dir": "/tmp/artc-robot-logs",
    "auto_start_session": true,
    "exit_when_session_ends": true,
    "audio_output": true,
    "test_tone_frequency": null,
    "camera_enabled": false
  }
}
```

机器人主机还需要：

- 阿里云 ARTC Linux SDK，默认放在 `robot/vendor/AliRTCSDK_Linux-7.11.0`。
- `parecord`，用于采集系统默认麦克风。
- `ffplay`，用于把客服声音播放到系统默认扬声器。

安装机器人 Python 依赖并启动：

```bash
conda activate rtc
python -m pip install -r requirements-robot.txt
chmod 600 config.json
python main.py robot
```

如果管理员创建机器人后还没有审批，在 `/admin.html` 点击“通过”。机器人会自动登录、
上报心跳、开始业务会话并加入自己的 ARTC 频道。

### 第 4 步：客服接入并验收

客服打开：

```text
http://127.0.0.1:8100/
```

使用管理员创建的客服账号登录。正常流程是：

1. 客服看到机器人会话显示“等待客服接入”。
2. 点击“开始处理”，会话变为“你正在处理”。
3. 其他客服此时看到“其他客服处理中”，不能重复接入。
4. 客服可以听取机器人声音。
5. 需要向机器人说话时，点击“开始接管”，再点击“开启麦克风”。
6. 机器人扬声器播放客服声音，同时机器人麦克风继续向当前客服回传。
7. 点击“释放接管”关闭客服麦克风。
8. 点击“离开会话”只释放占用，会话重新变为“等待客服接入”。
9. 点击“结束会话”会结束整次业务，机器人停止媒体并退出进程。下一次外部呼叫重新
   拉起机器人时，只验证数据库中已有的 Robot ID 和 Credential，不会重新注册。

首次部署完成的最低验收标准：

```text
控制端健康检查通过
机器人显示在线
客服能看到“等待客服接入”
第一名客服能独占接入
第二名客服看到“其他客服处理中”
客服能听到机器人
机器人扬声器能听到客服
双方说话时机器人麦克风和客服麦克风都未被另一方中断
离开后会话恢复“等待客服接入”
```

## 日常启动入口

控制端和机器人端运行在 Linux，要求 Python 3.10.x；客服 Web 服务可运行在当前客服
电脑的 Windows 上：

```text
python main.py
python main.py server
python main.py robot
python main.py agent
```

`python main.py agent` 会启动独立客服 Web 服务并打开页面。默认地址：

```text
http://127.0.0.1:8100/
```

直接运行 `python main.py` 会显示控制端、机器人端和 Web 客服工作台三个入口。首次
部署建议直接使用本文上面的显式命令，避免选错组件。配置从固定位置自动加载，不需要
在命令中传递 JSON 文件路径。

## 环境

- 控制端、机器人端：Linux x86_64。
- 客服工作台：Windows 10/11 或 Linux，浏览器使用 Chrome/Edge。
- Conda 环境 `rtc`，Python 3.10.x。
- 阿里云 ARTC Linux SDK 7.11.0，放在被忽略的 `robot/vendor/`。
- 机器人真实麦克风通过系统默认输入源和 `parecord` 采集，运行机需提供该命令。
- 客服工作台不需要 npm 或前端构建；由 Python 标准库启动独立静态服务。

```bash
conda activate rtc
python --version
```

Windows 客服电脑只需安装 Python 3.10.x；Web 工作台使用标准库启动，不需要 PySide6
或 npm。

## 统一配置

项目只读取根目录 `config.json`。该文件包含 AppKey 和机器人设备凭证，已加入
`.gitignore`，权限应为 `0600`。可提交模板为 `config.example.json`：

```text
cluster  共享控制端地址
server   控制端
agent    客服 Web 服务
robot    机器人端
```

从模板创建当前主机自己的 `config.json`：

```bash
cp config.example.json config.json
chmod 600 config.json
```

Windows PowerShell：

```powershell
Copy-Item config.example.json config.json
```

`config.json` 不应提交或分发给其他主机。每台主机可以只保留自己所需的节：
控制端保留 `server`，Windows 客服端保留 `cluster + agent`，机器人端保留
`cluster + robot`。只有一个文件是格式统一，不代表所有密钥要复制到所有机器。

不再读取 `artc.local.json`、`agent/config.json`、`robot/config.json`、
`*.config.local.json`，也不读取 `RTC_*`、`ARTC_*` 环境变量。

机器人和客服共享 `cluster.control_url`，不再重复配置 `backend_url`。控制端默认
`server.host` 为：

```json
"host": "0.0.0.0"
```

`0.0.0.0` 表示监听本机所有 IPv4 网卡。客户端仍必须通过控制端实际可达地址连接，
例如 `cluster.control_url` 使用 Tailscale IP，不能填写 `0.0.0.0`。如果只希望
监听 Tailscale，可将 `server.host` 改为 `tailscale`，启动时会解析并监听 Tailscale
IPv4。

## 组件详细说明

以下内容用于配置核对和故障排查。首次部署仍以上面的第 1 至第 4 步为准。

### 控制端说明

安装服务端依赖并初始化管理员：

```bash
python -m pip install -r requirements-server.txt
python main.py server --create-admin admin
python main.py server
```

数据库文件和表结构会在首次启动时自动创建，但不会自动生成任何账号、场景或机器人。
空数据库必须显式执行一次 `--create-admin`，并在隐藏提示中输入管理员密码。初始密码
不会写入 `config.json` 或数据库明文。

健康检查：

```bash
curl http://<控制端Tailscale-IP>:8000/api/health
```

控制端只从 `config.json` 的 `server` 节读取监听地址和端口，不再支持命令行
`--host`、`--port`、`--db` 覆盖。

### 机器人端说明

管理员通过 `/admin.html` 或管理 API 创建并审批机器人。机器人参数统一写入根目录
`config.json` 的
`robot` 节：

```json
{
  "robot": {
    "robot_id": "<robot-id>",
    "credential": "<设备凭证>",
    "sdk_root": "robot/vendor/AliRTCSDK_Linux-7.11.0",
    "log_dir": "/tmp/artc-robot-logs",
    "audio_output": true
  }
}
```

机器人连接地址来自 `cluster.control_url`，不在 `robot` 节重复配置。
`audio_output=true` 时机器人通过 `ffplay` 把客服语音播放到系统默认扬声器；
未配置时也默认启用；`false` 仅统计远端音频并丢弃，用于没有扬声器的降级环境。

推荐先由管理员创建机器人并保存一次性返回的 `Robot ID + Credential`，再填入机器人
配置。机器人登录后如果仍是待审批状态，会自动等待管理员处理。也可以在机器人配置中
预先设置 `robot_id + credential` 后启动，机器人发现凭证尚不存在时会自动注册为待审批
设备，管理员在 Windows 管理页审批后即可继续。

启动：

```bash
python -m pip install -r requirements-robot.txt
python main.py robot
```

机器人会自动登录、上报心跳、连接 WebSocket、开始可见业务会话、获取短期 ARTC Token
并加入独立频道。业务会话建立后机器人麦克风默认发布，客服是否听取不会改变该默认值。
当前项目不包含机器人 TTS/自动对话；人工接管只设置
`automaticSpeechPaused=true` 控制标记。外部业务语音模块如已接入，必须读取该标记并
自行暂停，机器人麦克风和客服听取不受影响。

`exit_when_session_ends=true` 时，客服结束业务会话后机器人进程自动退出。机器人身份、
审批状态和历史会话仍在控制端数据库中，下次启动只需登录验证；只有记录不存在时才会
进入自动注册流程。若由外部守护程序管理机器人生命周期，建议保持该值为 `true`。

若机器人提示“机器人凭证无效”，在**控制端 Linux 本机**运行只读诊断：

```bash
python -m tools.diagnose_robot_auth
```

控制端 `config.json` 没有 `robot` 节时，可只传机器人 ID：

```bash
python -m tools.diagnose_robot_auth --robot-id '<robot-id>'
```

需要核对旧进程使用的其他数据库时，可只读指定文件：

```bash
python -m tools.diagnose_robot_auth --db /tmp/artc-integration.db
```

`--db` 只用于诊断，不会改变项目配置或控制端启动时使用的数据库。

`robotExists=false` 表示控制端当前数据库没有该机器人；`robotExists=true` 且审批/启用
正常时，401 表示本地保存的凭证与服务端哈希不一致，需要重新轮换设备凭证并同步到
机器人配置。

该诊断命令会读取服务端数据库，不能在机器人端或 Windows 客服电脑上运行。机器人端
只通过 HTTP/WebSocket 访问控制端，不需要 SQLite。

### 客服端说明

管理员创建客服账号后（系统自动使用默认场景），客服启动独立工作台：

```bash
python main.py agent
```

- 客服页面：`http://127.0.0.1:8100/`
- 管理页面：`http://127.0.0.1:8100/admin.html`

管理页面支持机器人创建/查询/改名/删除/审批/启停/凭证轮换、客服创建/启停和审计记录。业务场景作为
内部兼容字段隐藏，不再要求管理员操作。管理员登录后使用同一控制端 API，普通客服
账号不能进入管理页面。
机器人删除会立即结束活动会话、注销设备凭证并从机器人目录隐藏；历史会话和审计仍保留。

客服 Web 配置位于根目录 `config.json` 的 `agent` 节。Windows 客服工作台监听本机
`127.0.0.1:8100`，连接地址来自 `cluster.control_url`：

```json
{
  "cluster": {
    "control_url": "http://<控制端Tailscale-IP>:8000"
  },
  "agent": {
    "host": "127.0.0.1",
    "port": 8100,
    "auto_open_browser": true
  }
}
```

浏览器和客服 Web 服务在同一台 Windows 电脑时，访问 `http://127.0.0.1:8100/`
属于浏览器可信的安全上下文，可以正常申请麦克风权限。客服页面来源是 localhost，
因此控制端现有 CORS 规则默认允许；机器人访问控制端不涉及浏览器 CORS。

Tailscale 部署时：

1. 控制端和机器人端 Linux 加入同一 tailnet。
2. Windows 客服电脑安装并登录 Tailscale。
3. 控制端 `server.host` 使用 `0.0.0.0`，通过 Tailscale IP 和防火墙限制访问。
4. `cluster.control_url` 使用控制端 Tailscale IP。
5. Windows 浏览器只访问本机 `127.0.0.1:8100`，不需要让 Windows 监听外部网卡。

在控制端本机可创建默认客服账号：

```bash
python -m tools.create_default_agent
```

该命令要求已经存在管理员，会自动创建或复用隐藏的默认场景，创建 `agent01`，随机
生成密码并只输出一次。数据库只保存密码哈希。

工作台支持：

- 查看有权限的业务会话状态：等待接入、正在处理、客服通话中或机器人离线。
- 客服接入会话前由后端原子认领；一个会话只允许一名客服接入，一个客服同时只占一个会话。
- 使用 ARTC Web SDK 接收机器人音频和可选摄像头画面。
- 申请、续租、释放人工接管租约。
- 默认关闭客服麦克风和摄像头，接管成功后按需发布客服麦克风。
- 左侧会话卡片显示“客服通话中/客服已接管”和“麦克风已开/麦克风关闭”。
- 机器人麦克风在活动业务会话中默认发布，客服端麦克风默认关闭。
- 离开或控制面断线时立即关闭本地麦克风，并释放会话认领、租约和监听。
- 短期 ARTC Token 到期前自动向业务后端刷新。

访问 Token 和 ARTC Token 只保存在页面内存，不写入 `localStorage`、Cookie 或 URL。
浏览器客服的 `userId` 由后端生成为 `agent-<客服业务 id>`。

ARTC Web SDK 固定为 `7.3.4`，文件 SHA-256：

```text
46b57790f4539150f45c6ab3f113e9ed0b73e756fd3c97a0d6813c99628ba6d1
```

## 测试

```bash
python -m unittest discover -s tests -v
python -m compileall -q server robot agent tools tests
python -m tools.secret_scan
```

自动化测试不访问真实 ARTC 云。真实跨 SDK 验收需要同时运行 Linux 机器人和控制端，
再启动独立 `agent` Web 服务，在浏览器中验证机器人音频、人工接管、客服麦克风、可选
摄像头、断线重连和 Token 刷新。

## 当前限制

- “Linux 机器人 + Web 客服”跨 SDK 真实媒体闭环尚未在本轮验收。
- 物理客服麦克风、机器人扬声器听感、可选摄像头和长时间运行仍需真实设备验证。
- 公网部署仍需 HTTPS/WSS、域名、证书、限流和账号安全配置。

接口和数据模型见 [docs/artc-architecture.md](docs/artc-architecture.md)，验收状态见
[docs/acceptance.md](docs/acceptance.md)，机器人 PoC 见
[docs/artc-poc.md](docs/artc-poc.md)，ARTC 频道、统计和记录边界见
[docs/artc-cloud-capabilities.md](docs/artc-cloud-capabilities.md)。
