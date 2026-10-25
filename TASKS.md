# ARTC 业务语音机器人与客服协作任务书

更新日期：2026-10-23

本任务书是当前 `artc` 分支的任务入口。当前媒体链路只使用阿里云 ARTC，
业务后端、机器人端、客服端、测试和工具均围绕这一链路维护。

## 1. 目标

构建一个支持多机器人、多应用场景、多客服的业务语音机器人与客服协作原型。

目标拓扑：

```text
Linux 机器人端
  ARTC Linux Python SDK
  业务语音会话、客服人工接管播放、默认机器人麦克风、可选摄像头

Linux 业务后端
  FastAPI + SQLite
  身份、业务场景、业务会话、机器人目录、ARTC Token、在线状态、人工接管锁、审计

Web 客服工作台
  ARTC Web SDK
  业务会话查看、客服人工接管、客服音频和浏览器媒体状态

阿里云 ARTC
  只负责实时音视频媒体传输
```

首版必须支持：

- 机器人注册、审批和启用/停用。
- 场景与机器人归属。
- 客服登录和场景访问权限。
- 机器人控制面在线状态。
- 机器人业务语音会话状态。
- 客服查看当前业务语音会话；摄像头按业务配置可选显示。
- 客服申请人工接管锁后开麦。
- 机器人通过扬声器播放客服声音。
- 空闲时机器人麦克风关闭，业务会话中按可见规则发布。
- 人工接管期间设置自动语音暂停标记；外部业务语音模块消费该标记，项目本身不包含 TTS。
- 多客服并发申请同一机器人人工接管时只有一个成功。
- 关键操作审计。

## 2. 当前状态

- 分支：`artc`。
- 当前工作树只保留 ARTC 客户端、业务后端、测试和工具。
- 当前实现为 Linux 机器人端、Linux 业务后端和浏览器 Web 客服工作台。
- 机器人使用 ARTC Linux Python SDK，客服使用 ARTC Web SDK 7.3.4。
- T1-T6 的代码能力已落地：接口/模型、业务后端、并发接管、机器人业务运行器、
  Web 客服工作台、管理员和审计。
- T7/T8 的自动化、联调、秘密检查和配置校验工具已交付。
- 原 Linux Python 客服端及其终端联调工具已移除。
- 顶层 `main.py` 保留控制端、机器人端和 Web 客服工作台三个独立入口；客服 Web 页面
  由 `agent` 静态服务托管，控制端只提供 REST/WebSocket。
- Linux 机器人 + Web 客服跨 SDK 真实音频闭环尚未完成验收。
- 当前机器没有可用声卡，真实麦克风、扬声器听感和摄像头尚未完成验收。

## 3. 不在本轮范围

- 屏幕采集和桌面共享。
- 摄像头录像、音频录制和云端存储。
- 机器人内部 ASR、NLU、对话模型和 TTS 实现。
- 自动拨号、短信通知或人工外呼。
- 公网正式上线、备案、证书自动化和高可用集群。
- PostgreSQL、Redis、消息队列、Docker 和多进程后端。
- 原生 Windows 媒体客户端；浏览器客服工作台不属于此项排除范围。

## 4. 不可破坏的约束

- 业务后端不转发音视频。
- 每台机器人使用独立 ARTC 频道。
- 机器人端和客服端只使用 ARTC Linux Python SDK。
- 机器人和客服使用不同、唯一且稳定的 ARTC `userId`。
- AppKey 只存在业务后端，不进入机器人端或客服端。
- 机器人端和客服端只接收短期 ARTC Token。
- 同一机器人同一时间只允许一个客服持有人工接管锁。
- 一个活动会话只允许一名客服原子认领；其他有权限客服只能看到占用者。
- 空闲时机器人麦克风关闭；活动业务会话中默认发布且状态必须可见。
- 客服麦克风默认关闭，获得人工接管锁后才可发布。
- 不允许隐蔽监听或隐藏采集。
- 机器人/客服界面必须显示麦克风、人工接管和可选的摄像头状态。

## 5. 核心业务流程

### 5.1 机器人上线

```text
机器人读取身份和场景配置
  -> 使用设备凭证登录业务后端
  -> 建立 WebSocket
  -> 周期上报心跳和媒体状态
  -> 业务后端标记机器人 online
  -> 客服端收到状态推送
```

### 5.2 客服查看业务语音会话

```text
客服登录业务后端
  -> 获取有权限的场景和机器人目录
  -> 选择一台正在执行业务会话的机器人
  -> 请求该机器人的观看 RTC Token
  -> ARTC Linux Python SDK 加入机器人频道
  -> 接收机器人业务语音
  -> 如业务启用摄像头，则同时显示机器人视频
```

### 5.3 客服人工接管

```text
客服点击“开始人工接管”
  -> 后端原子申请 takeover lease
  -> 成功后向机器人和客服广播 agent_takeover 状态
  -> 机器人暂停自动业务语音/TTS
  -> 客服开始发布麦克风音频
  -> 机器人接收客服音频并播放到扬声器
  -> 客服结束人工接管或租约超时
  -> 停止发布麦克风并释放租约
  -> 机器人恢复原业务语音状态
```

### 5.4 按需听取机器人侧语音

```text
客服点击“听取机器人语音”
  -> 认领会话并加入频道
  -> 接收活动会话中默认发布的机器人麦克风
  -> 客服收到机器人音频
  -> 客服点击“停止听取机器人语音”
  -> 离开或结束会话后停止接收
```

## 6. 建议数据模型

字段名和最终接口在实现前需要单独确认。

### `scenarios`

- `id`
- `name`
- `enabled`
- `created_at`
- `updated_at`

### `robots`

- `id`
- `display_name`
- `status`：`offline | online | degraded`
- `enabled`
- `credential_hash`
- `channel_id`
- `last_seen_at`
- `created_at`
- `updated_at`

### `scenario_robots`

- `scenario_id`
- `robot_id`
- 联合唯一约束

### `robot_sessions`

- `id`
- `robot_id`
- `status`：`active | agent_takeover | ended`
- `started_at`
- `ended_at`
- `end_reason`

机器人同一时间最多只能有一个未结束的业务语音会话。

### `agent_scenarios`

- `agent_id`
- `scenario_id`
- 联合唯一约束

### `takeover_leases`

- `id`
- `robot_id`
- `session_id`
- `agent_id`
- `status`：`active | released | expired`
- `acquired_at`
- `expires_at`
- `released_at`

同一 `robot_id` 只能存在一个 `active` 租约。

### `audit_events`

- `id`
- `actor_type`
- `actor_id`
- `action`
- `target_type`
- `target_id`
- `metadata_json`
- `created_at`

## 7. 建议业务接口

以下路径和字段只作为设计草案，实际实现前必须确认公共接口。

### 机器人

```text
POST /api/robot/login
POST /api/robot/heartbeat
POST /api/robot/rtc-token
POST /api/robot/media-state
POST /api/robot/sessions/start
POST /api/robot/sessions/{session_id}/end
POST /api/robot/disconnect
GET  /api/robot/ws-ticket
WS   /ws
```

### 客服

```text
POST /api/auth/login
GET  /api/scenarios
GET  /api/robots
GET  /api/robot-sessions
POST /api/robot-sessions/{session_id}/viewer-token
POST /api/robot-sessions/{session_id}/takeover-lease
POST /api/robot-sessions/{session_id}/takeover-lease/renew
POST /api/robot-sessions/{session_id}/takeover-lease/release
GET  /api/auth/ws-ticket
WS   /ws
```

### 管理员

```text
POST /api/admin/login
GET  /api/admin/robots
POST /api/admin/robots/{robot_id}/approve
POST /api/admin/robots/{robot_id}/disable
POST /api/admin/agents
POST /api/admin/scenarios
GET  /api/admin/audit
```

## 8. ARTC Token 边界

- 业务后端从根目录 `config.json` 的 `server` 节读取 ARTC 凭证。
- 机器人端和客服端只调用业务后端获取 Token。
- Linux Python SDK 使用单参数 Base64 Token。
- 生成 Token 时写入明确的过期时间。
- 客户端处理即将过期事件并刷新。
- Token 和对应频道/用户必须一致。
- 日志和测试输出必须脱敏。

## 9. 任务总览

| 任务 | 内容 | 依赖 | 完成门槛 |
|---|---|---|---|
| T0 | ARTC Linux 双端 PoC | 无 | 真实 Linux 语音互通，摄像头按范围验证 |
| T1 | 接口、数据模型和配置设计 | T0 | 用户确认公共接口和数据模型 |
| T2 | 业务后端基础能力 | T1 | 认证、场景、机器人目录和 Token 可用 |
| T3 | 在线状态与人工接管锁 | T2 | 并发认领和超时释放测试通过 |
| T4 | Linux 机器人端 | T0、T2、T3 | 业务语音、人工接管播放、心跳和默认机器人麦克风 |
| T5 | Web ARTC SDK 客服工作台 | T0、T2、T3 | 登录、会话查看、人工接管和跨 SDK 媒体闭环 |
| T6 | 管理端与审计 | T2、T3 | 审批、停用、场景权限和审计可用 |
| T7 | 自动化与真实设备验收 | T4、T5、T6 | 完整回归和真实设备验收 |
| T8 | 安全、验证和交付 | T7 | 完整回归和秘密检查 |

## 10. T0：ARTC Linux 双端 PoC

负责人：集成窗口。

目标：在进入业务开发前证明 Linux ARTC Python SDK 机器人和 ARTC Web SDK 客服能够
稳定互通。

准备：

- 阿里云 ARTC AppID、AppKey。
- Linux ARTC SDK 官方包。
- 机器人 Linux 进程和一个支持 ARTC Web SDK 的 Chrome/Edge 浏览器。
- 如验证真实听感，需要客服麦克风、耳机和机器人扬声器。
- 如果本轮包含视频，再准备 Linux 摄像头。
- 一个临时、不可提交的测试 Token 生成方式。

执行：

1. Linux Python SDK 初始化，必须设置 `h5mode=True`。
2. 机器人加入固定测试频道并发布业务语音。
3. Web 客服使用不同 userId 加入同一频道并接收机器人语音。
4. Web 客服申请人工接管租约后发布客服麦克风。
5. Linux 机器人接收客服音频并播放到扬声器。
6. Linux 机器人默认发布麦克风，Web 客服接收机器人侧语音。
7. 停止客服麦克风和机器人麦克风，确认状态正确。
8. 断开并重新加入频道。
9. 如果范围包含摄像头，后续在可用客户端中验证发布和显示。

验收：

- 双向音频可用。
- 机器人收到客服音频，Web 客服收到机器人音频，并看到对方上下线。
- 默认不发布麦克风，显式操作后才发布。
- 没有重复 `userId`。
- 视频和音频停止后没有残留。
- `AliRtcCoreService` 能正常退出。
- 记录 SDK 版本、系统版本、音频设备和错误码；有视频时记录摄像头信息。

当前结果：

- 官方 Linux SDK 7.11.0 初始化和释放通过。
- 原 Linux 客服端曾与机器人完成真实双向合成音频验证，相关代码已移除。
- Linux 机器人 + Web 客服跨 SDK、真实声卡、麦克风、扬声器和断线重连仍需验收。

失败处理：

- PoC 不通过时停止后续业务开发。
- 不改业务模型来掩盖媒体问题。
- 记录问题并回到用户决策。

## 11. T1：接口、数据模型和配置设计

负责人：后端/架构窗口。

工作：

1. 确认机器人、客服、管理员认证方式。
2. 确认场景、机器人、客服权限关系。
3. 确认 WebSocket 消息类型和快照格式。
4. 确认业务语音会话的开始、结束以及自动语音暂停标记的读取消流程。
5. 确认人工接管锁状态、租约时长和超时行为。
6. 确认 ARTC 频道和 `userId` 生成规则。
7. 确认数据库迁移方式。
8. 确认 ARTC Token 是否能够限制客服音频发布权限。如果 Token 仅认证频道和
   `userId`，必须明确人工接管锁是业务层约束，不能宣称达到密码学级别的发布权限隔离。
9. 更新 README 和架构图，使文档与实际 ARTC 链路一致。

验收：

- 数据结构能表达多场景、多机器人、多客服。
- 状态和错误场景有明确定义。
- 机器人业务语音会话使用独立状态模型。
- 公共接口变化已取得用户确认。

## 12. T2：业务后端基础能力

负责人：后端窗口。

工作：

1. 建立机器人注册、审批、启用和停用。
2. 建立客服登录和场景权限。
3. 实现 ARTC Token 接口。
4. 实现机器人心跳和在线超时。
5. 实现业务语音会话开始和结束。
6. 实现场景机器人目录和当前业务会话目录。
7. 实现 WebSocket 一次性票据和状态快照。
8. 实现审计记录。

自动化测试：

- 未登录访问被拒绝。
- 不同场景权限隔离。
- Token 不包含 AppKey。
- 心跳过期机器人在线状态变化。
- WebSocket 票据只能消费一次。

## 13. T3：并发人工接管锁

负责人：后端窗口。

工作：

1. 使用数据库写事务保证一个业务会话只有一个有效人工接管租约。
2. 实现申请、续租、释放和超时清理。
3. 客服断线时释放租约。
4. WebSocket 广播 `agent_takeover`、`released` 和 `expired`。
5. 机器人端收到接管状态后设置/清除自动语音暂停标记；外部 TTS 模块负责消费标记。

自动化测试：

- 10 个并发申请只有一个成功。
- 不同客服不能同时持有同一机器人租约。
- 一个客服不能绕过租约直接在界面开麦。
- 租约过期释放。
- 后端重启后不遗留永久锁。

## 14. T4：Linux 机器人端

负责人：机器人端窗口。

目录建议：

```text
robot/
```

工作：

1. 将官方 Linux ARTC SDK 二进制包放在不进版本控制的本地目录，记录版本和校验值。
2. 读取机器人配置和未跟踪的本地凭证。
3. 登录业务后端并获取 ARTC Token。
4. 维持 WebSocket 和心跳。
5. 初始化 ARTC Linux Python SDK。
6. 设置 `h5mode=True`。
7. 若业务配置启用视频，则按业务会话状态发布。
8. 机器人麦克风在空闲时关闭，并按业务会话状态发布。
9. 播放客服远端音频到本地扬声器。
10. 人工接管开始时设置自动语音暂停标记，结束时清除；项目不实现 TTS。
11. 活动业务会话中默认发布机器人麦克风，客服监听不再控制该发布状态。
12. 上报媒体、会话、错误和人工接管状态。

验收：

- 网络断开后重连和重新获取 Token。
- SDK 异常不会静默挂起。
- 客服人工接管在机器人扬声器可见可听。
- 不记录 AppKey。
- 机器人只在可见的活动业务会话中默认发布麦克风，空闲和会话结束后关闭。

## 15. T5：Web ARTC SDK 客服工作台

负责人：客服端窗口。

工作：

1. 使用阿里云 ARTC Web SDK 7.3.4，由独立 `agent` Web 服务托管。
2. 页面登录后只把访问 Token 和短期 ARTC Token 保存在内存。
3. 使用密码输入框登录，不在 URL 或本地存储中保存密码。
4. 显示并选择机器人当前业务语音会话。
5. 入会前关闭客服麦克风和摄像头发布。
6. 申请到人工接管锁后才允许发布客服麦克风。
7. 浏览器显示机器人音频、可选视频、接管和客服音频状态。
8. 提供续租、停止人工接管和退出频道。
9. Token 到期前通过业务后端刷新媒体鉴权信息。

测试：

- 登录失败和权限不足。
- 机器人离线。
- 人工接管锁冲突。
- 客服断线后锁释放。
- 客服端不持有 AppKey。
- ARTC Web SDK 文件哈希、客服静态服务和跨源接入。
- 入会前音频、视频发布均关闭。

## 16. T6：管理端与审计

负责人：客服端/后端窗口。

工作：

1. 管理员通过 Windows `/admin.html` 登录。
2. 机器人自注册展示和审批。
3. 机器人和场景归属。
4. 客服场景权限。
5. 机器人和客服停用。
6. 查看登录、上线、人工接管、释放和异常审计。
7. 提供一次性随机密码的默认客服初始化工具。

验收：

- 管理员才能修改审批和权限。
- 客服只能看到授权场景。
- 停用后已有凭证失效。

## 17. T7：自动化和真实设备验收

自动化命令：

```bash
python -m unittest discover -s tests -v
python -m compileall -q server robot agent tools tests
python -m tools.secret_scan
```

真实设备验收：

1. Linux 机器人上线并被客服看到。
2. 机器人开始一次可见的业务语音会话。
3. Web 客服接收到机器人业务语音。
4. 客服申请人工接管锁并说话，机器人扬声器播放。
5. 人工接管期间设置自动语音暂停标记，外部业务模块负责实际暂停。
6. 机器人默认发布麦克风，客服能够收到机器人侧语音。
7. 第二个客服只能查看，不能同时人工接管。
8. 客服断线后租约超时释放。
9. 机器人断线后客服看到离线，网络恢复后重新上线。
10. 长时间运行无进程和媒体残留。
11. 日志中无真实 Token、AppKey 和设备凭证。

## 18. T8：安全、验证和交付

工作：

1. 检查 AppKey、设备凭证和密码不进入客户端或版本库。
2. 验证 robot 连接 server，Web 客服跨源访问 server 的 REST/WebSocket。
3. 验证统一入口和固定位置配置加载。
4. 执行完整自动化回归。
5. 执行秘密扫描和补丁格式检查。

验收：

- AppKey 不在客户端文件和日志中。
- 后端可重启恢复。
- 机器人登录、业务会话、客服登录和人工接管有审计。
- 不允许隐蔽采集。

## 19. 新窗口启动提示

可以将下面文字交给新窗口：

```text
读取当前 AGENTS.md、TASKS.md 和 docs/acceptance.md。
当前 artc 分支包含 robot、server 和 agent/web。
控制端、机器人端和 Web 客服工作台保留三个独立入口；Web 页面由 agent 托管。
T1-T6 代码能力已落地，T7/T8 自动化和安全检查工具已提供；下一步优先完成真实声卡、
麦克风、扬声器、断线重连、摄像头和长时间运行验收。
客服端使用 ARTC Web SDK，不恢复 Linux Python 客服端，不要提交或推送。
完成后汇报实际使用环境、SDK 版本、命令、通过与未通过项。
```
