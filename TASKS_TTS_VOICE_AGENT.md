# 客服语音转文字再合成目标声音任务书

更新日期：2026-10-26

## 1. 任务目标

在当前 `codex/tts-voice-agent` 分支实现以下半双工流程：

```text
客服按住按钮说话
  -> 浏览器采集 16 kHz / 16-bit / 单声道 PCM
  -> 控制端 voice 模块执行阿里云实时 ASR
  -> 获取最终文本
  -> 阿里云 Qwen-Audio TTS 使用指定 voice_id
  -> TTS PCM 返回浏览器
  -> 浏览器构造 MediaStreamTrack
  -> 当前 ARTC Web SDK 客服连接发布目标声音
  -> 机器人扬声器播放
```

这不是实时语音转换，不保留客服原始声纹、语气和情绪。项目不接受 ASR 内容错误时
自动回退为客服原声。

## 2. 基线和工作边界

- 基线分支：`release-20261025(win-artc)`。
- 当前分支：`codex/tts-voice-agent`。
- 保留 `agent/native/` C++ 助手源码、构建脚本和预编译文件，不在本任务中删除。
- 默认部署路径不使用 C++ 助手；它只作为 Web SDK 自定义音轨 PoC 失败时的回退方案。
- 不新增独立 voice 服务进程，ASR/TTS 先作为 `server/` 内独立模块实现。
- 不新增数据库表，不修改现有核心数据模型。
- 不将 ASR/TTS 音频或文本发送给机器人业务模块。
- 不保存音频、完整识别文本、Token、密码或长期云凭证。
- 机器人端业务逻辑原则上不修改。

## 3. 架构

```text
Windows 客服端
  agent/web/
    - 现有登录、会话、接管和 ARTC Web SDK 接收能力
    - AudioWorklet 麦克风 PCM 采集
    - voice WSS 客户端
    - TTS PCM 播放缓冲和 MediaStreamTrack
    - ARTC Web SDK 自定义音频 Track 发布

Linux 控制端
  server/
    - 现有认证、权限、会话、接管租约、ARTC Token 和审计
    - voice_ai.py：一句话的状态编排
    - voice_asr.py：阿里云实时 ASR 适配
    - voice_tts.py：Qwen-Audio TTS 和 voice_id
    - voice_protocol.py：voice WSS 消息和二进制帧规则

Linux 机器人端
  robot/
    - 保持现有 ARTC 会话、机器人麦克风和扬声器播放逻辑
```

控制端负责 ASR/TTS 是因为云 API 凭证必须集中保存。控制端不得成为机器人 ARTC 媒体
中继；TTS 音频只返回发起请求的客服浏览器，由该浏览器通过 ARTC Web SDK 发布。

## 4. 必须先通过的 PoC 闸门

### 4.1 Web SDK 自定义音轨注入

在修改业务代码前，必须用测试音轨验证当前 ARTC Web SDK 7.3.4 能发布自定义
`MediaStreamTrack`：

```javascript
const context = new AudioContext();
const destination = context.createMediaStreamDestination();
const oscillator = context.createOscillator();

oscillator.connect(destination);
oscillator.start();

const track = destination.stream.getAudioTracks()[0];
await engine.switchMicrophone("", track);
await engine.publishLocalAudioStream(true);
```

验收：

- 机器人能听到测试音。
- 浏览器不打开真实麦克风。
- 停止发布后机器人不再收到音频。
- 重新发布不会残留旧 Track。

如果 PoC 失败，停止本方案实现，不能依赖 SDK 私有对象或修改压缩脚本。

### 4.2 阿里云 ASR/TTS 流式 PoC

PoC 必须使用官方当前 SDK 和文档，确认：

- 实时 ASR 能接收浏览器 PCM 流。
- 最终文本能稳定产生。
- Qwen-Audio TTS 支持流式音频输出。
- `longanqian_v3.1` 或声音复刻 `voice_id` 可用。
- TTS 输出能转换成固定采样率的 16-bit PCM。

若 TTS 只能整段返回或无法取得 PCM，记录实际格式与延迟，再决定由控制端解码还是
改用 Windows C++ 外部 PCM 发布。不得猜测供应商 API。

## 5. 控制端实现

### 5.1 配置

在未跟踪根目录 `config.json` 的 `server` 节增加可选 `voice_ai` 配置：

```json
{
  "server": {
    "voice_ai": {
      "enabled": true,
      "provider": "aliyun",
      "asr_api_key": "<secret>",
      "tts_api_key": "<secret>",
      "asr_model": "<verified-model>",
      "tts_model": "<verified-model>",
      "default_voice_id": "longanqian_v3.1",
      "max_utterance_seconds": 30,
      "max_tts_seconds": 60
    }
  }
}
```

实际字段必须按官方 SDK 和当前服务配置调整。密钥不得进入模板、日志、测试夹具或
浏览器。

### 5.2 WSS 入口

复用现有 `/api/auth/ws-ticket` 一次性票据，新增：

```text
WS /ws/voice?ticket=<one-time-ticket>
```

连接后客户端首先发送：

```json
{"type":"start","sessionId":"..."}
```

服务端必须校验：

- principal 类型为 agent。
- 客服有权访问该会话。
- 客服已认领会话。
- 客服持有未过期的有效 takeover lease。
- 该客服当前没有另一句话正在处理。

不需要为用户端开放永久凭证。voice WSS 每次连接使用新的一次性票据。

### 5.3 消息协议

客户端文本消息：

```text
{"type":"audio_end"}
{"type":"cancel"}
```

客户端二进制帧：

```text
16 kHz
16-bit little-endian
单声道
约 20 ms 一块
```

服务端文本事件：

```text
ready
asr.partial
asr.final
tts.started
tts.completed
cancelled
error
pong
```

`tts.started` 必须返回统一格式：

```json
{
  "type": "tts.started",
  "sampleRate": 48000,
  "channels": 1,
  "bitDepth": 16
}
```

`tts.started` 后服务端发送的二进制帧全部为 TTS PCM。每次说话只能有一个活动
utterance；重复 `start` 必须返回协议错误。

## 6. 客服 Web 实现

- 新增 AudioWorklet，采集并重采样为 16 kHz PCM。
- `startRecording` 前必须确认接管租约、voice WSS 和 ARTC 媒体连接可用。
- 按住期间发送二进制 PCM。
- 松开后发送 `audio_end`，停止真实麦克风 Track。
- 原始麦克风不得通过 Web SDK 发布。
- 收到 `tts.started` 后建立 TTS AudioWorklet 缓冲。
- TTS PCM 只进入 `MediaStreamAudioDestinationNode`，不默认播放到客服扬声器。
- 将生成的 AudioTrack 注入当前 ARTC Web SDK 客服连接。
- `tts.completed`、`cancel`、租约释放、页面关闭时必须停止 Track 并清空缓冲。

UI 状态：

```text
idle
recording
transcribing
synthesizing
publishing
cancelling
```

页面必须显示：

- 当前目标 voice_id。
- 是否正在录音。
- ASR 最终文本。
- TTS 是否正在发布。
- 取消按钮和可见错误。

## 7. 取消和清理

以下事件必须在 500 ms 内停止 ASR/TTS、清空 TTS PCM、停止自定义 Track 并关闭
ARTC 音频发布：

- 用户取消。
- ASR 文本为空。
- ASR 或 TTS 超时。
- 控制端 voice WSS 断开。
- 接管租约释放或过期。
- 浏览器页面关闭。
- ARTC 发布失败。

TTS 失败时不得回退原声。voice WSS 必须在 TTS 播放期间周期性检查接管状态。

## 8. 机器人端

- 保持现有 Linux ARTC 接收和扬声器播放。
- 接管期间暂停自动语音。
- 释放后恢复原业务状态。
- 不接收 ASR/TTS 凭证。
- 不保存、转写或转发客服音频。

## 9. 安全与隐私

- ASR/TTS 密钥只存在于控制端 `config.json`。
- 浏览器和机器人只获得短期票据。
- voice WSS 票据一次性使用。
- 每次 `start` 校验会话和租约。
- 不记录完整识别文本或 PCM。
- 音频、文本和 TTS 缓冲只存在于内存。
- TTS 播放期间持续校验租约。
- 页面必须清楚显示录音、合成、发布和取消状态。
- 禁止隐藏采集或静默切换原声。

## 10. 测试要求

自动化测试不得访问真实云服务，使用可替换的 ASR/TTS 假适配器。

必须覆盖：

- 未认领、无权限、无租约和过期租约被拒绝。
- 一次性票据不能重复使用。
- 同一客服并发发送两句话被拒绝。
- ASR 空结果、异常、超时。
- TTS 异常、超时、取消。
- TTS 播放期间租约释放后立即停止。
- 页面不会同时发布真实麦克风和 TTS Track。
- 取消后状态回到 idle，旧音频不能继续到达 ARTC。
- 日志和错误中不出现密钥、完整 Token 或完整文本。

## 11. 真实设备验收

1. Windows 客服登录、认领会话、取得接管租约。
2. 按住说话，松开后显示最终文本。
3. 机器人只播放一次目标声音。
4. 机器人听不到客服原始声音。
5. `longanqian_v3.1` 或 voice_id 与配置一致。
6. 连续发送 10 句话不重复、不串音。
7. 取消后 500 ms 内停止。
8. 接管释放后无残留音频、Track、ASR/TTS 任务。
9. 连续运行 10 分钟，内存和音频缓冲不持续增长。
10. 记录从松开按钮到机器人首次出声的实际延迟。

## 12. 实施顺序

1. 完成 Web SDK 自定义 Track PoC。
2. 完成阿里云 ASR/TTS 流式 PoC。
3. 实现 voice WSS 和控制端 voice 模块。
4. 实现浏览器录音、状态机和 TTS Track 发布。
5. 接入租约、取消和清理。
6. 补自动化测试。
7. 完成真实机器人验收。
8. 根据 PoC 和验收结果决定是否保留 Windows C++ 回退路径。

## 13. 停止条件

出现以下任一情况时停止实现并回到用户决策：

- Web SDK 无法稳定发布自定义 MediaStreamTrack。
- 阿里云 TTS 无法流式返回可用音频。
- ASR 最终文本质量或延迟不能满足业务。
- 必须新增数据库结构或大幅改变公共接口。
- 发现需要保存音频、文本或长期云密钥才能工作。
- 实际延迟无法被业务接受。
