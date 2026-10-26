# 阿里云语音换音色能力调研

更新日期：2026-10-26

## 1. 调研范围

本文只核对阿里云官方文档，回答以下问题：

1. Qwen-Audio-TTS 能否接收客服语音、返回识别文本或直接返回换音色音频。
2. Qwen-Audio-Realtime 能否只做语音识别，或稳定地逐字复述输入语音。
3. 阿里云是否公开提供实时 Voice Conversion：保留原话语内容、停顿、语气和节奏，
   只替换说话人音色。
4. 如果不存在上述公开 API，阿里云百炼现有服务能否组合出接近目标的方案。
5. `longanqian_v3.1` 在独立 Qwen-Audio-TTS 中是否明确可用。

## 2. 结论

### 2.1 没有找到公开的实时 Voice Conversion API

截至 2026-10-26，在阿里云帮助中心中未找到明确提供以下能力的公开服务或 API：

- 输入实时语音流；
- 保留客服原始发音内容、停顿、语速、语气和情绪；
- 仅替换为目标说话人音色；
- 实时输出替换音色后的语音流。

这是一项文档检索结论，不代表阿里云内部或线下商务方案一定不存在。

### 2.2 Qwen-Audio-TTS 只做文本转语音

Qwen-Audio-TTS 的 WebSocket 客户端协议中，启动任务时 `payload.input` 固定为空对象，
待合成文本通过 `continue-task` 的 `payload.input.text` 发送。协议没有客服音频输入字段，
也没有语音识别结果事件。

因此：

- 它不能识别客服声音；
- 它不能返回 ASR 结果；
- 它只能把收到的文本合成为目标音色音频；
- 服务端返回的 `original_text` 是 TTS 收到的输入句子，不是语音识别结果。

官方来源：

- [Qwen-Audio-TTS WebSocket API](https://help.aliyun.com/zh/model-studio/qwen-audio-tts-websocket-api)
- [Qwen-Audio-TTS 客户端事件](https://help.aliyun.com/zh/model-studio/qwen-audio-tts-client-events)
- [Qwen-Audio-TTS 服务端事件](https://help.aliyun.com/zh/model-studio/qwen-audio-tts-server-events)

### 2.3 Qwen-Audio-Realtime 是对话模型，不是 Voice Conversion

Qwen-Audio-Realtime 接收 16 kHz、16-bit、单声道 PCM，输出 24 kHz、16-bit、
单声道 PCM，并且可以配置输入音频转写。因此它确实能够返回输入语音的识别文本。

但官方将它定义为端到端实时语音交互/对话模型。音频输出是模型生成的回复，
不是输入音频只换音色后的副本。协议没有 `voice_conversion=true`、`repeat_input=true`
或类似保证逐字复述的参数。

通过系统指令要求模型“只复述、不回答”可以做实验，但属于提示词约束，不是官方能力保证。
模型仍可能改写、漏字、补充内容或调整表达，因此不能直接作为生产级 Voice Conversion。

官方来源：

- [Qwen-Audio 实时语音对话模型](https://help.aliyun.com/zh/model-studio/qwen-audio-realtime-user-guides)
- [Qwen-Audio Realtime 客户端事件](https://help.aliyun.com/zh/model-studio/fun-audiochat-client-events)
- [Qwen-Audio Realtime 服务端事件](https://help.aliyun.com/zh/model-studio/qwen-audio-realtime-server-events)
- [Qwen-Audio Realtime WebSocket API](https://help.aliyun.com/zh/model-studio/fun-audiochat-realtime-websocket-api)

### 2.4 ASR + TTS 是当前公开 API 中最稳定的替代路线

Fun-ASR-Realtime 负责把客服语音识别为原话，Qwen-Audio-TTS 再把同一文本用目标音色
重新合成。该路线可以保证业务控制点在“识别最终文本”上，不要求对话模型自行回答。

基本链路：

```text
客服 16 kHz PCM
  -> Fun-ASR-Realtime
  -> sentence_end=true 的最终原话
  -> Qwen-Audio-TTS
  -> 目标音色 PCM
  -> ARTC
  -> 机器人扬声器
```

但它不是严格实时 Voice Conversion：

- 需要等待 ASR 产生稳定文本；
- TTS 会重新生成语气、停顿和情绪；
- 客服原始声纹不会保留；
- 最终延迟必须通过真实链路测量。

Fun-ASR-Realtime 官方事实：

- 协议模型名为 `fun-asr-realtime`；
- 音频参数支持 `format=pcm`、`sample_rate=16000`；
- `result-generated` 同时包含中间结果和最终结果；
- `sentence_end=true` 表示该句最终结果；
- 最终结果是普通识别文本，不是音频。

官方来源：

- [Fun-ASR-Realtime WebSocket API](https://help.aliyun.com/zh/model-studio/fun-asr-realtime-websocket-api)
- [Fun-ASR-Realtime 客户端事件](https://help.aliyun.com/zh/model-studio/fun-asr-client-events)
- [Fun-ASR-Realtime 服务端事件](https://help.aliyun.com/zh/model-studio/fun-asr-server-events)

Qwen-Audio-TTS 官方事实：

- 输入通过 `continue-task` 发送纯文本；
- `format` 可取 `pcm`；
- `sample_rate` 可取包括 48000 Hz 在内的多个值；
- 音频通过 WebSocket binary 帧持续返回；
- `sentence-end` 事件包含输入文本、字级时间戳和用量信息。

### 2.5 声音复刻不是实时 Voice Conversion

百炼声音复刻允许上传参考音频，创建自定义音色，再把返回的 `voice_id` 用于后续
Qwen-Audio-TTS 或 Qwen-Audio-Realtime 合成。

它的作用是“从样本创建可复用音色”，不是把之后的每一段客服实时音频直接转换为该音色。

官方来源：

- [声音复刻](https://help.aliyun.com/zh/model-studio/voice-cloning-user-guide)

### 2.6 `longanqian_v3.1` 的可用范围仍不确定

阿里云官方文档明确写明 `longanqian_v3.1` 是
`qwen-audio-3.1-realtime-plus` 的默认音色：

- [Qwen-Audio 实时语音对话模型](https://help.aliyun.com/zh/model-studio/qwen-audio-realtime-user-guides)

但在独立的 Qwen-Audio-TTS 音色列表中，本次检索没有确认到同名音色：

- [Qwen-Audio-TTS 音色列表](https://help.aliyun.com/zh/model-studio/qwen-audio-tts-voice-list)

因此不能直接声称独立 Qwen-Audio-TTS 一定支持 `longanqian_v3.1`。必须使用实际
API Key 和 Workspace ID 发起一次测试合成；如果 API 拒绝该音色，就需要：

- 选择独立 TTS 中最接近的可用音色；
- 或使用有合法授权的参考音频创建复刻音色；
- 或只使用 Qwen-Audio-Realtime，并承担其会生成回答、无法保证逐字复述的风险。

## 3. 可选方案

### 方案 A：Fun-ASR-Realtime + Qwen-Audio-TTS

适合“内容必须由系统控制，不交给对话模型回答”的业务。

优点：

- 使用官方公开 API；
- 识别文本和合成文本边界清楚；
- 可审计文字链路；
- 能指定独立 TTS 支持的系统音色或复刻音色；
- 音频可输出为 PCM，便于接入浏览器和 ARTC。

限制：

- 句级准实时；
- 丢失原始语气和情绪；
- 目标音色必须在独立 TTS 中可用；
- 实际延迟需 PoC。

### 方案 B：Qwen-Audio-Realtime + “逐字复述”指令

这是阿里云平台内最接近“输入语音直接用 `longanqian_v3.1` 输出”的实验方案。

方法：

- 使用 push-to-talk；
- 配置 `voice=longanqian_v3.1`；
- 在 `instructions` 中要求逐字复述输入，不回答、不解释、不补充；
- 对比输入转写和输出字幕，检查是否漏字或改写；
- 测量首包和完整音频延迟。

风险：

- 没有官方 Voice Conversion 语义保证；
- 模型可能回答或改写；
- 输出内容和时长可能变化；
- 不适合直接作为准确率要求高的业务链路。

### 方案 C：真正的实时 Voice Conversion

如果必须保留客服原始语气、停顿和情绪，只能使用专门 VC 引擎。阿里云公开文档中
没有找到可直接使用的实时 VC API，因此需要另行确认：

- 阿里云是否存在未公开、需要商务开通的 VC 服务；
- 是否允许引入自托管 VC 模型；
- 是否具备 GPU、模型权重和目标音色授权；
- 首包延迟、稳定性和并发成本是否满足业务。

## 4. 建议的 PoC 顺序

1. 用实际 API Key 测试独立 Qwen-Audio-TTS 是否接受 `longanqian_v3.1`。
2. 如果不支持，试听独立 TTS 可用音色，选择最接近目标的声音。
3. 用 Fun-ASR-Realtime 和 Qwen-Audio-TTS 串起 5 至 10 句真实客服语音。
4. 测量“客服说完一句”到“机器人第一次出声”的延迟。
5. 如果业务必须保留原始语气，再单独验证 Qwen-Audio-Realtime 逐字复述实验。
6. 在 PoC 未通过前，不修改正式业务链路，也不删除现有 ARTC 预设变声回退方案。

## 5. 尚未确认事项

- 独立 Qwen-Audio-TTS 是否对当前账号开放 `longanqian_v3.1`。
- 独立 TTS 中最接近 `longanqian_v3.1` 的可用系统音色。
- Fun-ASR-Realtime + Qwen-Audio-TTS 的真实端到端延迟。
- Qwen-Audio-Realtime 在“逐字复述”指令下的实际漏字率和改写率。
- 阿里云是否存在需要商务渠道开通的实时 Voice Conversion 服务。
