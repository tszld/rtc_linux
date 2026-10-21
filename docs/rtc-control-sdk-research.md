# RTC 音视频与控制通道 SDK 调研

更新日期：2026-10-20

## 结论

存在可以在音视频之外携带控制消息的 RTC SDK，但“消息通道统一”不等于“业务控制面消失”。

本项目当前使用 DingRTC Web SDK 3.9.36。DingRTC 另提供 `@dingrtc/rtm` 实时消息 SDK，
可以承载广播消息、点对点消息和成员进出事件。配合 DingRTC 使用时，RTM 可以注册到
RTC 实例并共享同一个入会连接。

但仍需要可信代码生成 RTC Token，并维护客户端审批、客服空闲状态、FIFO 队列、原子
派单、断线恢复和会话审计。RTM 只替代消息传输，不提供这些业务语义。

## DingRTC RTM

官方接入方式：

```javascript
import DingRTC from "dingrtc";
import RTM from "@dingrtc/rtm";

const client = DingRTC.createClient();
const rtm = new RTM();

// RTM 与 RTC 共享同一个入会连接。
client.register(rtm);

await client.join({
  appId,
  userName,
  channel,
  uid,
  token,
});
```

官方文档说明：

- 支持独立使用，也支持与 DingRTC 共享频道连接。
- 支持 Session 广播和指定 UserID 点对点消息。
- 支持 Session 成员进入、离开事件。
- 当前限制为每秒最多 60 条消息，单条不超过 4 KB。

来源：

- https://help.aliyun.com/zh/document_detail/2878536.html
- https://help.aliyun.com/zh/document_detail/2640158.html

## 其他可选能力

### 阿里云 ARTC Web SDK

ARTC Web SDK 提供 Data Channel，可在成功加入频道后发送自定义文本或二进制数据。
官方限制为消息发送给频道内所有订阅数据通道的用户，单条最大 64 KB。

来源：

- https://g.alicdn.com/apsara-media-box/imp-web-rtc-docs/7.1.10/api/media/data.html
- https://help.aliyun.com/zh/live/quick-use

ARTC 与当前 DingRTC 属于不同产品线和不同客户端接口，不能视为当前 SDK 的无缝升级。

### 腾讯云 TRTC

TRTC Web SDK 提供 `sendCustomMessage`，但官方将其定位为弱网下尽力可靠的消息能力，
单条最大 1 KB，每秒最多 30 次，只适合简单状态同步和提示。

来源：

- https://web.sdk.qcloud.com/trtc/webrtc/v5/doc/zh-cn/TRTC.html#sendCustomMessage

### LiveKit

LiveKit Room SDK 提供 reliable 和 lossy Data Packet，并可使用 LiveKit Cloud 托管媒体。
仍需应用后端或云函数签发 Access Token，并维护排队和派单逻辑。

来源：

- https://docs.livekit.io/home/client/data/packets/
- https://docs.livekit.io/home/server/generating-tokens/

## 完全托管的客服方案

如果目标是不自己实现客服状态、排队和路由，应选择完整客服联络中心 SaaS，而不是裸 RTC
SDK。腾讯云联络中心官方文档描述了技能组、坐席状态、空闲路由和呼入排队等能力。

来源：

- https://cloud.tencent.com/document/product/679
- https://cloud.tencent.com/document/product/679/73532
- https://cloud.tencent.com/document/product/679/84674

采用此类方案前必须验证单向屏幕共享、Web 嵌入、国内网络、费用和数据合规是否符合本项目。

## “旁路”的准确含义

RTC 中的“旁路”通常指旁路直播或旁路转推：把 RTC 房间中的媒体转推到 CDN、RTMP、
HLS 或 FLV，供普通直播观众观看。它不是应用控制消息通道。

来源：

- https://cloud.tencent.com/document/product/647/16826

本项目需要的控制能力应称为：

- RTM
- 实时消息
- 应用层信令
- RTC Data Channel
- 业务控制面

## 对本项目的建议

1. 当前内网原型继续保留 FastAPI + SQLite + WebSocket。
2. 若公网部署希望去掉自己的 WebSocket，可使用 DingRTC RTM 传递实时状态。
3. Token 签发、FIFO 队列、原子派单和持久化仍应放在可信服务中，可迁移到 Serverless
   或托管数据库。
4. 若不希望维护任何排队和派单逻辑，应评估完整客服联络中心 SaaS。
