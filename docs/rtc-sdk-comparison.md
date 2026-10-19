# 阿里云 RTC/ARTC/DingRTC SDK 对照

更新日期：2026-10-19

## 结论

当前项目使用 **DingRTC Web SDK 3.9.36**，属于 RTC 产品下的浏览器端媒体 SDK。

用户提供的视频直播页面属于 **Live 产品下的实时音视频 ARTC SDK**。ARTC 的
Android、iOS、Windows、macOS、Linux、HarmonyOS 和 Web 包是同一产品线的不同平台
实现，不是同一个二进制 SDK 简单改名。它们与当前项目的 DingRTC Web SDK 也是不同
产品线、不同 API 和版本体系。

`rtc/2018-01-11` 则是阿里云 OpenAPI 门户中的服务端 API 版本，用于云服务管理，
不是媒体端 SDK。

## 对照表

| SDK/API | 所属层级 | 当前代表接口/包 | 主要职责 | 典型场景 |
|---|---|---|---|---|
| DingRTC Web SDK | RTC 端 SDK | `dingrtc`、`window.DingRTC`、`DingRTCClient` | 浏览器/WebEngine 中采集、发布、订阅和播放媒体 | 呼叫中心、在线会议、一对一通话 |
| DingRTC Windows SDK | RTC 原生端 SDK | `DingRTC_Windows_SDK_3_9_0.zip`、`DingRTC.dll` | Windows C/C++ 应用的音视频客户端能力 | Windows 原生会议/客服客户端 |
| ARTC Android/iOS SDK | Live/ARTC 原生端 SDK | `AliVCSDK_ARTC` AAR/framework | 移动端直播互动、连麦和实时音视频 | 直播连麦、主播/观众互动 |
| ARTC Windows SDK | Live/ARTC 原生端 SDK | `AliRTCSdk.lib` 等 | Windows C++ 实时音视频客户端 | 视频直播互动、Windows 原生连麦端 |
| ARTC Web SDK | Live/ARTC Web 端 SDK | `aliyun-rtc-sdk`、`AliRtcEngine` | 浏览器实时音视频、自动发布/订阅和观看远端流 | 直播网页连麦、互动直播 |
| ARTC Linux SDK | Live/ARTC 原生端 SDK | `.so`、`AliRtcCoreService` | Linux 原生音视频，提供 C++、Java、Python、Go 接入 | Linux 客户端、服务器侧媒体接入 |
| RTC OpenAPI 2018-01-11 | 云服务管理 API | `api.aliyun.com` 中的 RTC API | 服务端调用阿里云 OpenAPI 管理资源 | 服务端自动化、资源/频道管理等 |

## 关键区别

### DingRTC Web SDK

- 当前项目文件记录的 SDK 版本是 `3.9.36`。
- 通过 `createClient()`、`join()`、`publish()`、`subscribe()` 等显式轨道接口工作。
- 音频使用 MCU 音频流，视频和屏幕共享属于用户轨道；本项目通过
  `subscribe(uid, "video", true)` 订阅呼叫端屏幕。
- 运行在普通浏览器和 PySide6 `QWebEngineView` 中。
- 与当前后端本地生成的 DingRTC Token 配套。

官方资料：

- https://help.aliyun.com/zh/document_detail/2674351.html
- https://help.aliyun.com/zh/document_detail/2640158.html

### ARTC SDK 各平台包

- 属于“视频直播 LIVE > 直播+ > 实时音视频”产品文档。
- Android 使用 `AliVCSDK_ARTC` AAR/Maven，iOS 使用 CocoaPods/framework，
  Windows 使用原生库，Web 使用标准 JavaScript SDK。
- Linux 是原生引擎加 `.so` 和 `AliRtcCoreService`，官方提供 C++、Java、
  Python、Go 的接入方式。
- ARTC Web 的公开接口是 `AliRtcEngine` / `aliyun-rtc-sdk`，不是当前项目的
  `DingRTCClient` / `dingrtc`。
- ARTC Web 默认在加入频道后自动采集、发布并订阅媒体；相机流和屏幕流通过
  `setRemoteViewConfig` 选择显示。
- ARTC 明确提供 `communication` 通话模式和 `interactive_live` 互动直播模式，
  以及 `interactive`/`live` 角色。

官方资料：

- https://help.aliyun.com/zh/live/artc-download-the-sdk
- https://help.aliyun.com/zh/live/introduction-to-real-time-audio-and-video-artc-sdk
- https://help.aliyun.com/zh/live/quick-use
- https://help.aliyun.com/zh/live/artc-sdk-for-linux-python-fast-access

### RTC OpenAPI 2018-01-11

- `api.aliyun.com` 是 OpenAPI 门户。
- `2018-01-11` 是云 API schema 版本，不是 WebRTC 协议版本或端 SDK 版本。
- 服务端 OpenAPI 负责调用阿里云云服务接口，不负责浏览器/客户端媒体采集、
  推流、订阅和播放。
- PyPI 的 `aliyun-python-sdk-rtc` 属于阿里云 OpenAPI Python SDK，不是音视频媒体
  客户端 SDK。

官方资料：

- https://api.aliyun.com/document/rtc/2018-01-11/overview
- https://pypi.org/project/aliyun-python-sdk-rtc/

## 平台与语言

ARTC 原生 Windows SDK 和 DingRTC Windows SDK 都提供 C/C++ 原生库。Windows
页面没有提供对应的 Python 音视频媒体客户端 SDK。

ARTC 官方提供 **Linux Python 接入**，但需要特别区分：

- 它不是可以通过 `pip install` 获得的纯 Python SDK。
- Python 接口随 Linux ARTC SDK 包分发，依赖 `AliRtcCoreService`、
  `libAliRtcLinuxEngine.so` 等原生文件。
- 音视频回调是 PCM/YUV 等裸数据，业务层需要自行播放、渲染或编码。
- 它与 Web SDK 互通时需要初始化 `h5mode=True`。
- 该方案不能直接运行在 Windows。

官方 Windows 集成文档：

- DingRTC Windows:
  https://help.aliyun.com/zh/document_detail/2640095.html
- ARTC Windows:
  https://help.aliyun.com/zh/live/artc-download-the-sdk
- ARTC Linux Python:
  https://help.aliyun.com/zh/live/artc-sdk-for-linux-python-fast-access

因此在 Windows 上，Python 不能通过安装一个官方包直接替代当前的 Web SDK。
可行的原生路线是由 C++ 媒体进程承载 SDK，再通过 IPC 与 Python 业务层通信。

## 互通与迁移边界

官方资料分别描述 DingRTC 和 ARTC 产品线，并未提供“直接将 `dingrtc` npm 包替换为
`aliyun-rtc-sdk`”的迁移说明，也未在以上页面声明两者可以无配置地加入同一房间。
实际项目不应混用两套 AppID、Token、事件名和轨道模型。

若要切换到 ARTC，需要重新核对：

- Live 控制台中的应用凭证。
- ARTC 的 Token/鉴权计算。
- `AliRtcEngine` 的入会、角色和频道模式。
- 自动发布/订阅策略及屏幕流显示接口。
- 后端房间 ID、凭证和前端事件模型。

## 本项目选择

当前需求是呼叫端与客服端一对一通话，并由客服查看呼叫端屏幕。DingRTC Web SDK
已经满足这些需求，并且现有后端、Token、显式音频订阅和屏幕共享代码已经围绕它完成。

ARTC 更偏向直播互动场景，增加观众/主播角色、直播模式或 CDN 生态时更合适。仅就
当前呼叫中心原型而言，继续使用 DingRTC Web SDK 的迁移风险更低。
