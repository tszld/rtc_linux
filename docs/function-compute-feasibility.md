# 阿里云 Function Compute 控制端迁移可行性

评估日期：2026-10-26

## 结论

当前 `release-20261026(FC)` 分支没有 FC 部署文件或适配层，`server` 也不能原样部署到
FC Agent Sandbox 后作为生产控制端使用。

可行的做法分两级：

1. **低风险增量方案**：保留现有 Linux 控制端，把不依赖长期连接和本地状态的 REST
   能力拆到 FC Web Function。
2. **整体 FC PoC**：使用 FC Web Function 或自定义容器运行 FastAPI，限制为单实例，
   将 SQLite 放到 NAS，增加 FC 定时触发清理，并接受最长 24 小时 WebSocket 强制断开。
   该方案可做验证，但不适合直接作为生产架构。

如果目标是正式迁移整个控制面，必须先引入外部事务数据库和外部发布订阅/实时通道。
这会改变当前项目“SQLite、无 Redis、单个长期运行进程”的核心约束，需要单独决策。

## 当前代码事实

- `FC` 分支目前没有 `Dockerfile`、`s.yaml`、`bootstrap` 或其他 FC 部署入口。
- Python 环境为 `3.10.22`，与 FC Python 3.10 要求兼容。
- `requirements-server.txt` 只有 FastAPI、Pydantic、Uvicorn 和 websockets；
  当前 `rtc` 环境还包含 PySide6 等无关的大型依赖，不能把整个 Conda 环境直接打包。
- `server.app` 依赖进程内 `WebSocketManager`、内存连接集合和 `asyncio` 后台清理循环。
- 数据库是本地 SQLite WAL，路径来自 `server.db_path`，启动时自动建表和迁移。
- ARTC Token 只使用标准库生成，属于容易迁移的无状态逻辑。
- AppKey 当前只从项目根目录 `config.json` 读取，不能写入函数镜像或代码包。

## 官方能力与限制

| 能力 | 官方结论 | 对本项目的影响 |
|---|---|---|
| FC Web Function | 执行超时默认 60 秒，最长 86400 秒 | 可以运行 FastAPI/Uvicorn，但 WebSocket 最长也只能保持配置的执行超时 |
| WebSocket | HTTP 触发器支持 WebSocket；连接超过函数执行超时会收到 1006 并强制关闭 | 机器人和客服必须保留自动重连；不能把连接视为永久连接 |
| WebSocket 实例 | FC 无状态，不保证同一客户端的多个请求进入同一实例 | 当前进程内 `WebSocketManager` 不能跨实例广播，需要 Redis 等外部通道 |
| 后台任务 | 调用返回后实例会被冻结，后台线程、协程和异步日志不能假设继续运行 | 当前进程内 `sweep_loop` 不能原样依赖，需要 FC 定时触发器 |
| 本地磁盘 | 磁盘属于当前实例，实例回收后数据消失；持久化需 NAS、OSS 等 | 当前 SQLite 不能放在普通临时磁盘 |
| FC Agent Sandbox | 面向 AI Agent、代码执行和临时服务；应用应主动终止 Sandbox | 可用于短期部署验证，不适合作为长期权威控制端 |
| FC Agent Sandbox 状态 | 官方说明该产品仅做维护，新业务建议使用 Agent Sandbox | 不应把新生产控制面直接建立在 FC Agent Sandbox 上 |
| Sandbox 生命周期 | 创建后持续占资源，直到主动终止、超时回收或白名单暂停 | 长期在线运行会持续占用资源和产生费用 |

官方来源：

- [使用 Web 函数快速创建 Web 应用](https://help.aliyun.com/zh/functioncompute/creating-a-web-function)
- [配置 HTTP 触发器并使用 WebSocket 触发](https://help.aliyun.com/zh/functioncompute/configure-an-http-trigger-for-a-function-that-is-triggered-by-websocket-requests-1)
- [配置实例生命周期](https://help.aliyun.com/zh/functioncompute/function-instance-lifecycle)
- [FC Agent Sandbox 产品简介](https://help.aliyun.com/zh/functioncompute/product-overview-of-fc-agent-sandbox)
- [FC Agent Sandbox 使用约束](https://help.aliyun.com/zh/functioncompute/usage-constraints-of-fc-agent-sandbox)
- [沙箱函数使用约束](https://help.aliyun.com/zh/functioncompute/sandbox-functions-using-constraints)

## 模块迁移判断

| Server 模块 | FC 可行性 | 条件 |
|---|---|---|
| `/api/health` | 高 | 无状态 |
| ARTC Token 生成 | 高 | AppKey 必须来自 FC Secret 或受控挂载，不能进镜像 |
| 场景、机器人、会话只读查询 | 高 | 数据库必须在 FC 外 |
| 管理员、客服、机器人认证 | 中 | 依赖共享数据库和 Token 表 |
| 认领、接管租约、会话状态转换 | 中 | 必须有支持事务和唯一约束的外部数据库 |
| 机器人/客服 WebSocket 推送 | 低 | 需要外部发布订阅或固定单实例实时服务 |
| 心跳超时和租约超时扫描 | 中 | 改为 FC 定时触发，不能依赖常驻线程 |
| 本地 SQLite + WAL | 低 | NAS 可保存文件，但不适合作为多实例在线 SQLite |

## 推荐实施顺序

### 阶段 1：FC 无状态 API PoC

只迁移以下能力：

- `/api/health`
- ARTC 短期 Token 生成
- 不依赖 WebSocket 的目录和只读接口

现有 Linux 控制端继续负责 SQLite、心跳、WebSocket、会话和接管租约。这样可以先验证
FC 网络、鉴权、日志、冷启动和调用费用，而不破坏控制面一致性。

### 阶段 2：整体 FC PoC

若要验证完整 API：

- 使用 FC Web Function 或自定义容器，监听 `0.0.0.0:9000`。
- 将函数执行超时设为所需值，最高不超过 86400 秒。
- 设置单实例和单实例多并发，避免进程内 WebSocket 状态跨实例分裂。
- SQLite 挂载 NAS，并在上线前验证 WAL、锁、实例重启和文件一致性。
- 将 `sweep_loop` 改为 FC 定时触发器。
- 客户端启用 WebSocket 超时重连。
- AppKey 不进入代码包或镜像，使用 FC Secret 或受控 NAS 文件。

该 PoC 只能视为验证环境，不能据此宣称生产级高可用。

### 阶段 3：生产迁移

生产迁移至少需要：

- 支持事务、唯一约束和并发锁的外部数据库。
- 跨实例的 WebSocket 发布订阅或独立实时通道。
- FC 定时触发的状态清理任务。
- Secret 管理和最小权限 RAM 角色。
- 调用、并发、WebSocket 时长和存储费用监控。

这些变化超出当前 MVM 范围，也违反当前“SQLite、无 Redis”约束，必须单独确认。

## 以取消云电脑为目标

如果“不要云电脑”指不维护一台常驻 Linux 主机，那么大方向是可行的，但要区分两种实现。

### 方案 A：单实例 FC + NAS SQLite

这是最容易做出的 PoC：

- FC Web Function 运行现有 FastAPI。
- WebSocket 执行超时设置为业务可接受的上限，最长 86400 秒。
- 最大实例数限制为 1，避免进程内 WebSocket 状态分裂。
- SQLite 文件放在 NAS。
- 心跳、租约和在线状态清理由 FC 定时触发。

它确实不需要云电脑，但仍有以下问题：

- FC 实例不是永久主机，重启后内存连接和后台任务都会丢失。
- 所有 WebSocket 和 REST 请求都受到单实例容量限制。
- WebSocket 达到执行超时后每 24 小时至少断开一次。
- NAS 上的 SQLite 仍需验证实例重启和并发写入。
- 为保持可用而设置最小实例数、长 WebSocket 和 NAS，费用不一定低于小型云主机。

该方案适合技术验证，不建议直接承载正式业务。

### 方案 B：完全无服务器控制面

这是能够取消云电脑且相对合理的生产方向：

- FC HTTP 函数承载认证、机器人目录、会话控制和 ARTC Token。
- 事务数据库承载用户、机器人、会话、认领和接管租约。
- 云原生 API Gateway WebSocket 或独立托管实时通道承载长连接。
- 外部发布订阅传递接管、会话和机器人状态事件。
- FC 定时触发器负责心跳、租约和离线清理。
- FC Secret/环境变量承载 ARTC AppKey。

这会替换当前三个核心实现：

- 本地 SQLite -> 云数据库。
- 进程内 WebSocketManager -> 托管 WebSocket/发布订阅。
- 常驻 sweep_loop -> FC 定时触发器。

该项目当前明确禁止 PostgreSQL、Redis、消息队列和多 worker 后端，因此方案 B 必须
作为一次核心架构决策单独确认，不能作为普通重构直接实施。

云原生 API Gateway 官方提供 WebSocket API 和路由能力，可作为候选入口；连接注册、
鉴权、主动推送和 FC 集成仍需做一次真实 PoC 验证：

- [云原生 API Gateway WebSocket API](https://help.aliyun.com/zh/api-gateway/cloud-native-api-gateway/user-guide/websocket-api/)
- [创建 WebSocket API](https://help.aliyun.com/zh/api-gateway/cloud-native-api-gateway/user-guide/create-a-websocket-api)

## 成本判断

“不用云电脑”不等于一定更便宜。FC 按调用、实例运行和 WebSocket 连接时长计费；
若长期保留最小实例、长连接、NAS 和数据库，成本可能与小规格云主机接近甚至更高。

对本项目当前规模，最经济的路径通常是：

1. 先保留控制端主机，完成媒体和业务验收。
2. 把 ARTC Token、健康检查和只读目录接口迁到 FC。
3. 统计实际在线时长、并发客服数和 WebSocket 时长。
4. 数据证明常驻主机成本明显高于托管组合后，再做完全无服务器化。

## 建议

不建议把 `server` 整体部署到 FC Agent Sandbox。

建议先采用阶段 1，把 ARTC Token 和健康检查等无状态接口迁移成 FC Web Function。
如果业务要求彻底 Serverless 化，再单独立项评估外部数据库与实时消息基础设施。
