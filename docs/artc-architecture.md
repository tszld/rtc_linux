# ARTC 业务协作平台设计（T1）

更新日期：2026-10-23。

本文确定 T1 的接口、数据模型、状态和配置。机器人端和控制端部署在 Linux；机器人
使用阿里云 ARTC Linux Python SDK，客服 Web 服务可运行在 Windows 或 Linux，
媒体由浏览器中的 ARTC Web SDK 处理。

## 1. 运行组件与 ARTC

```text
robot/   业务会话、心跳、机器人媒体、人工接管暂停/恢复
server/  身份、场景、机器人、业务会话、租约、Token、WebSocket、审计
agent/   独立托管客服页面、管理页面和 ARTC Web SDK
ARTC     仅认证频道/用户并传输媒体
```

业务后端不接收、转发、录制或保存音频和视频。ARTC Token 只认证频道和用户，
人工接管独占性由 SQLite 事务和后端租约保证。

## 2. 身份与认证

### 客服/管理员

- 用户名和密码登录。
- 密码使用 `hashlib.scrypt` 加盐哈希，数据库不保存明文。
- 登录成功返回随机不透明 Bearer Token；数据库只保存 SHA-256 哈希。
- Token 默认有效期 12 小时；账号停用后鉴权立即拒绝。
- 角色：`agent`、`admin`。只有 `admin` 可修改审批、权限、账号和场景。
- 管理员账号只能通过服务器 CLI 初始化或由现有管理员创建。
- Web 客服只把访问 Token 保存在页面内存，不使用 `localStorage` 或 Cookie。
- `/admin.html` 提供机器人审批、客服账号和审计操作；场景字段默认隐藏。

### 机器人

- 管理员创建机器人时生成一次性显示的随机设备凭证。
- 机器人可提交自注册申请，注册后为待审批状态；管理员审批后才能登录。
- 数据库只保存设备凭证哈希。
- 机器人使用 `robotId + credential` 登录，获得短期 Bearer Token。
- 未审批或停用的机器人不能登录、开始会话或获取 ARTC Token。

### WebSocket

- 客户端先用 Bearer Token 调用 HTTP 接口申请一次性票据。
- 票据默认 30 秒有效且只能消费一次。
- WebSocket 路径统一为 `/ws?ticket=...`，不接受长期访问 Token。

## 3. 场景与权限

- 一个场景包含多个机器人和多个客服。
- 机器人必须归属且只归属一个场景。
- 客服可拥有多个场景权限。
- 客服只能查看已授权场景下的机器人、会话、媒体状态和租约。
- 管理员可查看所有场景。

## 4. 核心数据表

### `scenarios`

- `id TEXT PRIMARY KEY`
- `name TEXT UNIQUE NOT NULL`
- `enabled INTEGER NOT NULL`
- `created_at TEXT NOT NULL`
- `updated_at TEXT NOT NULL`

### `agents`

- `id TEXT PRIMARY KEY`
- `username TEXT UNIQUE NOT NULL`
- `password_hash TEXT NOT NULL`
- `display_name TEXT NOT NULL`
- `role TEXT NOT NULL`：`agent | admin`
- `enabled INTEGER NOT NULL`
- `created_at TEXT NOT NULL`
- `updated_at TEXT NOT NULL`

### `agent_scenarios`

- `agent_id TEXT NOT NULL`
- `scenario_id TEXT NOT NULL`
- 联合主键，外键级联删除。

### `robots`

- `id TEXT PRIMARY KEY`
- `display_name TEXT NOT NULL`
- `scenario_id TEXT NOT NULL`
- `channel_id TEXT UNIQUE NOT NULL`
- `credential_hash TEXT NOT NULL`
- `status TEXT NOT NULL`：`offline | online | degraded`
- `enabled INTEGER NOT NULL`
- `approved INTEGER NOT NULL`
- `last_seen_at TEXT`
- `deleted_at TEXT`：软删除时间；目录查询隐藏，历史数据和审计保留。
- `created_at TEXT NOT NULL`
- `updated_at TEXT NOT NULL`

### `robot_sessions`

- `id TEXT PRIMARY KEY`
- `robot_id TEXT NOT NULL`
- `status TEXT NOT NULL`：`active | agent_takeover | ended`
- `claimed_by_agent_id TEXT`
- `claimed_at TEXT`
- `started_at TEXT NOT NULL`
- `ended_at TEXT`
- `end_reason TEXT`
- 同一机器人最多一个未结束会话，由 SQLite 部分唯一索引保证。
- 一个未结束会话最多一名认领客服；一个客服最多认领一个未结束会话，均由部分唯一
  索引保证。

### `takeover_leases`

- `id TEXT PRIMARY KEY`
- `robot_id TEXT NOT NULL`
- `session_id TEXT NOT NULL`
- `agent_id TEXT NOT NULL`
- `status TEXT NOT NULL`：`active | released | expired`
- `microphone_published INTEGER NOT NULL`：当前接管客服是否已发布麦克风。
- `acquired_at TEXT NOT NULL`
- `expires_at TEXT NOT NULL`
- `released_at TEXT`
- `end_reason TEXT`
- 同一机器人最多一个 `active` 租约，由 SQLite 部分唯一索引保证。

### `robot_media_state`

- `robot_id TEXT PRIMARY KEY`
- `session_id TEXT`
- `microphone_published INTEGER NOT NULL`
- `camera_published INTEGER NOT NULL`
- `automatic_speech_paused INTEGER NOT NULL`
- `updated_at TEXT NOT NULL`

### `session_listeners`

- `session_id TEXT NOT NULL`
- `agent_id TEXT NOT NULL`
- `enabled INTEGER NOT NULL`
- `updated_at TEXT NOT NULL`
- 联合主键为 `(session_id, agent_id)`。
- 活动会话最多绑定一名已认领客服。认领客服离开、断线或账号停用后清除认领。
- 监听记录仍按 `(session_id, agent_id)` 保存，但写入监听前必须持有该会话认同权。

### `access_tokens`

- `id TEXT PRIMARY KEY`
- `principal_type TEXT NOT NULL`：`agent | robot`
- `principal_id TEXT NOT NULL`
- `token_hash TEXT UNIQUE NOT NULL`
- `expires_at TEXT NOT NULL`
- `revoked_at TEXT`
- `created_at TEXT NOT NULL`

### `ws_tickets`

- `id TEXT PRIMARY KEY`
- `principal_type TEXT NOT NULL`
- `principal_id TEXT NOT NULL`
- `ticket_hash TEXT UNIQUE NOT NULL`
- `expires_at TEXT NOT NULL`
- `consumed_at TEXT`
- `created_at TEXT NOT NULL`

### `audit_events`

- `id INTEGER PRIMARY KEY AUTOINCREMENT`
- `actor_type TEXT NOT NULL`
- `actor_id TEXT`
- `action TEXT NOT NULL`
- `target_type TEXT NOT NULL`
- `target_id TEXT`
- `metadata_json TEXT NOT NULL`
- `created_at TEXT NOT NULL`

## 5. 业务状态

### 机器人连接状态

```text
offline <-> online -> degraded -> offline
```

- 心跳未超过配置超时为 `online`。
- 心跳超过软超时但仍可能重连时为 `degraded`。
- 主动断开、停用或超过硬超时为 `offline`。
- ARTC 媒体状态不能替代心跳状态。

### 业务会话

```text
active <-> agent_takeover
   |
   +-> ended
```

- `start` 时机器人在线、已审批、已启用且没有未结束会话。
- `agent_takeover` 只由有效租约认领产生。
- 租约释放、过期或客服断线后恢复 `active`。
- 会话结束后再次开始创建新会话。
- 客服“离开会话”只释放认领，不结束业务会话；只有机器人请求或客服显式“结束会话”
  才结束业务并让机器人离开 ARTC 频道。

## 6. 人工接管租约

- 默认 60 秒，允许续租，单次申请最长 300 秒。
- 申请、续租、释放、过期清理全部使用 `BEGIN IMMEDIATE` 写事务。
- 同一机器人只允许一个 `active` 租约；数据库部分唯一索引是最终防线。
- 客服 WebSocket 断开时释放其所有 active 租约。
- 租约过期时广播 `takeover_expired`，机器人清除自动语音暂停标记。项目本身不包含
  TTS/对话引擎；外部业务语音模块需读取该标记并自行暂停或恢复。
- 获取租约的客服才能发布麦克风；后端 Token 本身不提供发布权限隔离。

## 7. HTTP API

### 通用

```text
GET  /api/health
POST /api/auth/login
POST /api/admin/login
GET  /api/auth/me
POST /api/auth/logout
GET  /api/auth/ws-ticket
WS   /ws?ticket=...
```

### 机器人

```text
POST /api/robot/login
GET  /api/robot/me
POST /api/robot/heartbeat
POST /api/robot/rtc-token
POST /api/robot/media-state
POST /api/robot/sessions/start
POST /api/robot/sessions/{session_id}/end
POST /api/robot/disconnect
GET  /api/robot/ws-ticket
```

### 客服

```text
GET  /api/scenarios
GET  /api/robots
GET  /api/robot-sessions
POST /api/robot-sessions/{session_id}/claim
POST /api/robot-sessions/{session_id}/claim/release
POST /api/robot-sessions/{session_id}/viewer-token
POST /api/robot-sessions/{session_id}/takeover-lease
POST /api/robot-sessions/{session_id}/takeover-lease/renew
POST /api/robot-sessions/{session_id}/takeover-lease/release
POST /api/robot-sessions/{session_id}/takeover-lease/microphone
POST /api/robot-sessions/{session_id}/listen
```

### 管理员

```text
GET  /api/admin/robots
POST /api/admin/robots
PATCH  /api/admin/robots/{robot_id}
DELETE /api/admin/robots/{robot_id}
POST /api/admin/robots/{robot_id}/approve
POST /api/admin/robots/{robot_id}/reject
POST /api/admin/robots/{robot_id}/disable
POST /api/admin/robots/{robot_id}/enable
PUT  /api/admin/robots/{robot_id}/scenario
POST /api/admin/robots/{robot_id}/rotate-credential
GET  /api/admin/agents
POST /api/admin/agents
POST /api/admin/agents/{agent_id}/disable
POST /api/admin/agents/{agent_id}/enable
PUT  /api/admin/agents/{agent_id}/scenarios
GET  /api/admin/scenarios
POST /api/admin/scenarios
POST /api/admin/scenarios/{scenario_id}/disable
POST /api/admin/scenarios/{scenario_id}/enable
GET  /api/admin/audit
```

所有 JSON 响应字段使用 camelCase；数据库字段使用 snake_case。

## 8. WebSocket 消息

服务端消息：

```text
snapshot
robot_status
robot_updated
robot_deleted
session_started
session_ended
session_claimed
session_released
media_state
agent_takeover
takeover_released
takeover_expired
takeover_microphone_changed
listen_changed
error
pong
```

客服客户端消息：

```text
ping
```

机器人客户端消息：

```text
ping
```

消息统一包含 `type`；状态变化包含 `robotId`、`sessionId`、`occurredAt`，按事件补充
状态和操作者。WebSocket 不传媒体内容、AppKey 或完整长期 Token。

## 9. ARTC 身份与 Token

- 每台机器人独立 `channelId`。
- 机器人 ARTC userId：`robot-<business robot id>`。
- 客服 ARTC userId：`agent-<business agent id>`。
- userId 最长 64 字符，只包含字母、数字、`-`、`_`。
- 后端用 `config.json` 的 `server.app_id/server.app_key` 生成短期单参数 Token。
- 默认有效期 600 秒，HTTP 响应返回 `expiresAt`。
- AppKey 不进入响应、数据库、客户端日志或审计。
- Linux Python SDK 当前包未暴露 Token 刷新接口；机器人到期前需重新获取 Token 入会。
- Web SDK 在 `authInfoWillExpire` 时重新请求短期 Token，并调用
  `refreshAuthInfo`；刷新失败时停止客服发布并离会。

## 10. 配置

项目只读取未跟踪的根目录 `config.json`，模板是 `config.example.json`。不再读取
`RTC_*`、`ARTC_*` 环境变量，也不读取组件目录下的旧配置文件。

```json
{
  "cluster": {
    "control_url": "http://100.64.0.1:8000"
  },
  "server": {
    "host": "0.0.0.0",
    "port": 8000,
    "app_id": "<AppID>",
    "app_key": "<AppKey>"
  },
  "agent": {
    "host": "127.0.0.1",
    "port": 8100,
    "auto_open_browser": true
  },
  "robot": {
    "robot_id": "<robot-id>",
    "credential": "<credential>"
  }
}
```

- `cluster.control_url` 是机器人端和客服端共享的控制端地址。
- `server.host="0.0.0.0"` 时监听所有 IPv4 网卡；客户端仍通过实际 Tailscale IP 或
  域名访问。`server.host="tailscale"` 仍可作为只监听 Tailscale IPv4 的受限选项。
- `server.cors_origins` 只用于非 localhost 的额外客服页面来源。
- `config.json` 包含密钥，必须保持未跟踪和 `0600`；各主机只应保存自己所需的节。

默认客服页面地址为 `http://127.0.0.1:8100/`。Windows 客服浏览器和客服 Web 服务
同机时使用 localhost；控制端通过 Tailscale IP 或域名访问。两者是独立进程，通过
HTTP Bearer Token 和 WebSocket 票据通信。

## 11. 数据库迁移

- 初始 schema 使用 `PRAGMA user_version = 1`。
- 启动时在事务内按版本递增迁移。
- SQLite 启用 `foreign_keys=ON`、WAL 和 `busy_timeout`。
- 应用启动时不自动清空未结束会话或租约；启动恢复任务将过期租约标记为
  `expired`，并释放对应会话。

## 12. 错误语义

- `400`：字段、状态或参数错误。
- `401`：未登录、Token 过期或设备凭证错误。
- `403`：场景越权、机器人未审批/停用、管理员权限不足。
- `404`：对象不存在或对当前客服不可见。
- `409`：重复会话、接管冲突或租约已被他人持有。
- `429`：登录或高频操作限制。
- `503`：ARTC 配置缺失或媒体服务暂不可用。
