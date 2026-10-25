# 阿里云 ARTC 云端能力与记录边界

更新日期：2026-10-23

本文只讨论视频直播产品中的“实时音视频 ARTC”，不混用 DingRTC、RTC OpenAPI
或互动直播消息。ARTC 服务端 OpenAPI 命名空间为 `live/2016-11-01`。

## 结论

- ARTC 有频道管理 API，但它是运行时控制接口，不是机器人目录或完整房间 CRUD。
- 频道不需要预先创建。官方明确说明：频道由 RTC 客户端 SDK 入会时隐式创建。
- 同一 AppID 下可以有多个频道。多机器人可以共用一个 AppID，每台机器人使用不同的
  `ChannelId`，不需要为每台机器人创建独立应用。
- 同一个 AppID 内 `UserId` 必须全局唯一。机器人应使用稳定的
  `robot-<业务机器人ID>`，不能只保证频道内唯一。
- 云端可以查询当前在线频道、频道在线用户、踢出用户和关闭频道。
- 云端保存控制和历史数据，但必须区分在线状态、回调投递记录、质量指标和聚合用量；
  它们不能替代业务数据库。
- 音视频内容不会自动保存。云端录制是独立收费能力，需主动启停并配置 OSS 或 VOD。

## 频道生命周期

官方 `DeleteChannel` 文档写明：

> 频道由 RTC 客户端 SDK 入会时隐式创建，可通过 `ListRTCLiveRooms` 查询当前存在的
> 频道。

来源：
[DeleteChannel](https://help.aliyun.com/zh/live/developer-reference/api-live-2016-11-01-deletechannel)

因此本项目不需要先在阿里云“创建频道”。机器人拿到服务端签发的 Token 后，使用
`ChannelId + UserId + Token` 入会，频道由第一个成功入会的客户端触发创建。

频道关闭后不影响以后继续使用同一个频道名称。`DeleteChannel` 会让当前频道内所有
成员退出：

来源：
[DeleteChannel](https://help.aliyun.com/zh/live/developer-reference/api-live-2016-11-01-deletechannel)

ARTC 只知道 AppID、ChannelID、UserId 和媒体事件。它不知道业务机器人名称、场景、
客服权限、接管关系或业务会话结束原因；这些必须由业务控制端维护。

## 频道管理 API

官方服务端 API 索引列出的 ARTC 频道管理能力包括：

- [ListRTCLiveRooms](https://help.aliyun.com/zh/live/developer-reference/api-live-2016-11-01-listrtcliverooms)：
  分页查询指定 AppID 下当前活跃的在线频道；这是在线列表，不是历史频道列表。
- [DescribeChannelParticipants](https://help.aliyun.com/zh/live/developer-reference/api-live-2016-11-01-describechannelparticipants)：
  分页查询指定频道内当前在线用户 ID。
- [DescribeChannelUsers](https://help.aliyun.com/zh/live/developer-reference/api-live-2016-11-01-describechannelusers)：
  查询当前互动者、观众的 ID 和人数。
- [RemoveTerminals](https://help.aliyun.com/zh/live/developer-reference/api-live-2016-11-01-removeterminals)：
  从频道踢出最多 30 个用户。
- [DeleteChannel](https://help.aliyun.com/zh/live/developer-reference/api-live-2016-11-01-deletechannel)：
  关闭整个频道。
- [CreateRTCWhipStreamAddress](https://help.aliyun.com/zh/live/developer-reference/api-live-2016-11-01-creatertcwhipstreamaddress)：
  创建向指定频道推流的 WHIP 地址，主要面向 OBS 等外部推流工具。

官方 API 索引：
[实时音视频服务端 API](https://help.aliyun.com/zh/live/server-api-reference)

这些 API 是阿里云 OpenAPI，调用方需要阿里云 RAM 身份并授予对应
`live:*` 权限。它们使用阿里云账号身份，不负责本项目客服账号认证。

### 在线用户不等于业务在线状态

`DescribeChannelUsers` 文档说明：频道最后一个用户离开后，接口可能仍有若干秒缓存，
返回 `IsChannelExists=true`，但互动者和观众人数都为 0。

来源：
[DescribeChannelUsers](https://help.aliyun.com/zh/live/developer-reference/api-live-2016-11-01-describechannelusers)

因此 ARTC 频道在线状态只能作为媒体侧辅助证据，不能替代机器人业务心跳，也不能直接
作为人工接管锁。

## 事件回调与云端记录

需要频道开启/关闭、用户入会/离会、发布/停止发布等细节时，必须配置 ARTC 事件回调。

`CreateEventSub` 支持：

- `ChannelEvent`：`Open`、`Close`
- `UserEvent`：`Join`、`Leave`、开始/停止发布音频、视频、屏幕共享、角色切换

来源：
[CreateEventSub](https://help.aliyun.com/zh/live/developer-reference/api-live-2016-11-01-createeventsub)

每个 AppID 最多同时创建 20 个订阅，并且只允许 1 个全频道订阅。机器人数量很多时，
应优先使用一个全频道订阅，而不是为每台机器人创建单独订阅。

回调失败时，ARTC 会按递增间隔重试 7 次；每次重试都会产生单独的回调记录。因此接收端
必须按 `MsgId` 做幂等处理。

配置订阅后，可通过 `ListEventSubEvent` 查询回调记录。该接口的限制是：

- 只能查询最近 7 天
- 最大查询跨度 7 天
- 最小查询粒度 1 分钟

来源：
[ListEventSubEvent](https://help.aliyun.com/zh/live/developer-reference/api-live-2016-11-01-listeventsubevent)

如果未配置事件订阅，或者回调接收服务没有把事件写入自己的数据库，则不能把云端回调
查询当作长期业务会话记录。需要长期保存时，应由业务后端接收回调并落库。

## 质量和通话记录

ARTC 质量观测控制台提供：

- 应用级全局质量趋势
- 频道排行
- 通话列表
- 单通通话分析
- 用户入会/离会、发布、订阅等事件流水

控制台通话列表可查询最大 30 天、跨度最大 30 天的数据；频道指标排行也有相同的
30 天限制。

来源：
[质量指标监控](https://help.aliyun.com/zh/live/quality-service)

公开服务端 API 目前明确提供的是
[DescribeLiveInteractionMetricData](https://help.aliyun.com/zh/live/developer-reference/api-live-2016-11-01-describeliveinteractionmetricdata)：

- `JoinChannelSucRate`：5 秒入会成功率
- `VideoStuckRate`：视频卡顿率
- `AudioStuckRate`：音频卡顿率
- `FirstFrameCost`：首帧耗时
- 可按 AppID 查询，也可选填 ChannelId
- 只能查最近 30 天，单次跨度不超过 24 小时，粒度 5 分钟

官方服务端 API 索引中没有发现与“质量控制台完整历史通话列表”等价的、可按时间列出
全部历史频道和参与者的公开 API。因此：

- 控制台可以看到近 30 天通话和用户事件流水。
- 程序化 API 可确认查询的是质量指标、当前在线状态或 7 天回调记录。
- 不能仅凭现有公开 API 索引宣称可以长期程序化导出完整业务会话和参会人员历史。

## 用量记录

`DescribeLiveGrtnDuration` 可查询连麦/ARTC 通话时长用量：

- 最大查询范围：最近 90 天
- 单次查询跨度：不超过 31 天
- 最小粒度：5 分钟
- 返回：AppID、音视频类型、媒体规格、时间片和时长

来源：
[DescribeLiveGrtnDuration](https://help.aliyun.com/zh/live/developer-reference/api-live-2016-11-01-describelivegrtnduration)

返回结果按 AppID 和时间片聚合，不包含本项目的机器人 ID、客服 ID、业务场景或接管
记录。不能直接用它还原“哪个客服何时接管了哪台机器人”。

## 云端录制

ARTC 音视频不会默认保存。云端录制是独立收费功能，需要主动启停任务，并把结果保存到
OSS 或 VOD。

来源：
[ARTC 云端录制](https://help.aliyun.com/zh/live/cloud-recording)

当前项目没有配置或调用云端录制，因此不会在阿里云生成音视频文件。

## Token 与控制端边界

ARTC 官方要求由业务服务端使用 AppID/AppKey 生成 Token，再下发给客户端。客户端使用
Token、AppID、ChannelID、UserId 等信息入会。

来源：
[Token 鉴权](https://help.aliyun.com/zh/live/token-based-authentication)

频道管理 OpenAPI 不能代替本项目的：

- 机器人目录和场景归属
- 客服账号与场景权限
- 获取机器人对应频道的短期 Token
- 人工接管原子锁
- 机器人业务心跳
- 业务会话状态、释放原因和审计

## 对本项目的建议

1. 继续保留一个控制端，用它保存机器人目录、客服权限、Token 签发和接管锁。
2. 一个 AppID 支持多频道；机器人继续使用独立 `ChannelId`，不要每台机器人建一个
   AppID。
3. 如果只需要“当前是否有媒体连接”，可接入 `ListRTCLiveRooms` 和
   `DescribeChannelUsers` 做辅助核对。
4. 如果需要“入会、离会、推流开始/停止”事件，配置一个全频道 `EventSub`，由控制端
   验签、幂等接收并写入业务数据库。
5. 需要长期保存时，以业务数据库为准；ARTC 的 7 天回调记录、30 天质量和 90 天用量
   只能作为云端证据或对账来源。
6. 阿里云 OpenAPI 需要单独的 RAM 授权，不能把阿里云 AccessKey 下发到机器人或
   Windows 客服浏览器。
