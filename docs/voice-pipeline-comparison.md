# 本地 ASR + Qwen TTS 与豆包声音复刻 2.0 对比

更新日期：2026-10-26

## 1. 要解决的问题

目标是把客服说出的原话转换成目标音色，再由机器人扬声器播放。

需要区分两个业务语义：

1. **内容保持的 TTS 重说**：先识别客服原话，再让 TTS 用目标音色朗读识别文本。
2. **严格实时 Voice Conversion**：保留客服原始内容、停顿、语速和情绪，只替换音色。

本对比中的“本地 ASR + Qwen-Audio-TTS”和“豆包声音复刻 2.0”都属于第 1 类，
不是第 2 类。

## 2. 结论

### 2.1 本地 ASR + Qwen-Audio-TTS 可以实现

建议链路：

```text
客服浏览器
  -> 16 kHz PCM
  -> Linux 控制端 server/voice
  -> 本地实时 ASR
  -> sentence_end=true 的最终原话
  -> Qwen-Audio-TTS
  -> 目标音色 PCM
  -> 客服浏览器 MediaStreamTrack
  -> ARTC
  -> Linux 机器人扬声器
```

这条链路的关键优点是识别文本由自己的 ASR 产生，再原样交给 TTS，不经过对话模型回答。

它不是严格实时 Voice Conversion：

- 必须等待本地 ASR 产生稳定文本；
- TTS 会重新生成语气、停顿和情绪；
- 本地 ASR 的准确率和延迟会影响整条链路；
- `longanqian` 是否可用仍需实际调用 Qwen-Audio-TTS 验证。

### 2.2 本地 ASR 不一定比阿里云 ASR 更好

本地 ASR 的潜在收益：

- 客服原始音频不必上传到阿里云 ASR；
- 少一次 ASR 云端网络往返；
- ASR 模型、版本和运行环境完全由自己控制。

本地 ASR 的代价：

- 需要新增 ASR 运行时、模型文件和模型许可证；
- 需要 CPU 或 GPU 资源；
- 需要自行处理模型升级、热词、噪声、方言和准确率；
- 本地推理不一定比云端更快；
- 文本最终仍要上传给 Qwen TTS，因此不能消除云端依赖。

如果当前没有明确的隐私或延迟硬约束，先使用阿里云
`Fun-ASR-Realtime + Qwen-Audio-TTS` 的接入成本更低。只有在 PoC 证明云端 ASR
延迟或数据边界不可接受时，再引入本地 ASR 更合理。

### 2.3 豆包声音复刻 2.0 不是实时 Voice Conversion

豆包“声音复刻 2.0”的官方最佳实践描述的是 ICL 声音复刻流程：

- 使用 14 至 30 秒的单人音频作为 prompt；
- 让复刻模型学习参考音频的音色特征；
- 后续在 TTS 合成时使用复刻得到的音色。

官方最佳实践还说明，`context_texts` 用于改善合成时的情感表现，而不是把输入语音
原样转换为另一音色。

官方来源：

- [声音复刻 2.0 最佳实践](https://docs.volcengine.com/docs/DoubaoVoice/SoundReplication20BestPractices?lang=zh)

豆包声音复刻 API V3 的流程也是：

```text
参考音频
  -> voice_clone 接口
  -> 生成 speaker_id
  -> 使用 speaker_id 调用 TTS
  -> 输入文本，输出合成音频
```

其中 V3 WebSocket 双向流式接口支持“文本实时流式输入、流式输出音频”，
但输入仍是文本，不是客服实时语音。

官方来源：

- [声音复刻 API V3](https://docs.volcengine.com/docs/DoubaoVoice/SoundReplicaAPI-V3?lang=zh)
- [声音复刻 SDK 概览](https://docs.volcengine.com/docs/DoubaoVoice/SDKOverview-2?lang=zh)

因此，豆包声音复刻 2.0 适合“创建一个定制 TTS 音色”，不适合直接声称可以完成
保留原始语气的实时 Voice Conversion。

## 3. 能力对比

| 对比项 | 本地实时 ASR + Qwen TTS | 阿里云 Fun-ASR + Qwen TTS | 豆包声音复刻 2.0 + TTS |
|---|---|---|---|
| 核心能力 | 语音识别后 TTS 重说 | 语音识别后 TTS 重说 | 参考音频创建 TTS 音色 |
| 是否严格实时 VC | 否 | 否 | 否 |
| 是否保留原始语气 | 否 | 否 | 否 |
| 是否保留原话内容 | 由 ASR 决定 | 由 ASR 决定 | 由 ASR 决定；TTS 本身不识别语音 |
| 目标音色 | Qwen TTS 可用音色 | Qwen TTS 可用音色 | 由授权参考音频训练的音色 |
| 是否可用 `longanqian_v3.1` | 待 API 实测 | 待 API 实测 | 文档未说明可直接选择该音色 |
| ASR 网络链路 | 本地/内网推理 | 阿里云 WebSocket | 取决于另行选择的 ASR |
| 部署复杂度 | 最高，需要 ASR 模型运行时 | 最低，复用已有 `websockets` | 中高，新增火山引擎 API、音色训练和计费 |
| 数据边界 | 原始音频留在本地，文本发给 Qwen | 音频和文本进入阿里云 | 参考音频与合成文本进入火山引擎 |
| 延迟风险 | 本地推理 + TTS | ASR 网络 + TTS | ASR + TTS；流式 TTS 降低音频首包等待 |
| 内容可控性 | 高，文本边界明确 | 高，文本边界明确 | 高，文本边界明确 |
| 主要新增依赖 | 本地 ASR 模型和运行时 | 现有 `websockets` 即可 | 火山引擎 API/SDK、账号与音色资源 |

## 4. 关于 `longanqian`

阿里云官方明确说明 `longanqian_v3.1` 是
`qwen-audio-3.1-realtime-plus` 的默认音色，但独立 Qwen-Audio-TTS 的音色列表
中没有确认到同名项。

官方来源：

- [Qwen-Audio 实时语音对话模型](https://help.aliyun.com/zh/model-studio/qwen-audio-realtime-user-guides)
- [Qwen-Audio-TTS 音色列表](https://help.aliyun.com/zh/model-studio/qwen-audio-tts-voice-list)

因此，不能直接假设“ASR + Qwen-Audio-TTS”一定能输出这个音色。必须先做一次
最小 TTS 调用验证。

如果独立 TTS 不支持该音色，可选路径是：

1. 在独立 TTS 中选择最接近的可用音色。
2. 使用有合法授权的目标音色样本创建复刻音色。
3. 实验 Qwen-Audio-Realtime 的“逐字复述”指令，但它不是官方保证的 Voice Conversion。

## 5. 豆包声音复刻的适用场景

豆包声音复刻 2.0 更适合以下场景：

- 已经拥有合法授权的目标说话人样本；
- 目标是建立长期可复用的品牌客服音色；
- 能接受 ASR 后 TTS 的句级延迟；
- 不要求保留客服原始语气和情绪；
- 愿意新增火山引擎账号、密钥、音色槽位和计费。

它不适合以下场景：

- 必须逐帧保留客服原始语调和停顿；
- 必须复用 Qwen 平台内部系统音色而不能再取得音频样本；
- 不接受客服原话先经过 ASR 再重新合成；
- 无法承担跨云平台的运维和网络复杂度。

## 6. 建议

### 如果优先降低实现风险

先使用阿里云 `Fun-ASR-Realtime + Qwen-Audio-TTS`：

- 不需要先部署本地 ASR；
- 可以尽快验证 `longanqian` 是否可用；
- 可以尽快测量最终文本到机器人首次出声的真实延迟；
- 能保留现有 ARTC 和 Windows 预设变声回退。

### 如果原始音频不能上传

再引入本地实时 ASR：

- 浏览器只把客服音频送到自己的 Linux 控制端；
- 本地 ASR 输出文本；
- 只把文本发送给 Qwen-Audio-TTS；
- 机器人仍通过 ARTC 接收合成音频。

本地 ASR 需要通过单独 PoC 证明准确率和延迟优于阿里云 ASR，不能因为“本地”
就默认更快或更好。

### 如果必须使用豆包复刻音色

需要把方案调整为：

```text
客服语音
  -> 选择阿里云或本地 ASR
  -> 最终文本
  -> 豆包复刻音色 TTS
  -> PCM/WAV 音频
  -> 客服浏览器 MediaStreamTrack
  -> ARTC
  -> 机器人扬声器
```

这仍然不是实时 Voice Conversion，并且不能与现有阿里云 ARTC Token、账号和音色
体系直接共用；它是新的第三方服务边界，需要用户单独确认后再引入。

## 7. 推荐的 PoC

1. 用同一段客服语音分别测试阿里云 Fun-ASR 和候选本地 ASR。
2. 比较最终文本准确率、`sentence_end` 延迟和资源占用。
3. 用同一批文本测试 Qwen-Audio-TTS，确认 `longanqian_v3.1` 是否可用。
4. 测量“客服停止说话”到“机器人首次出声”的完整延迟。
5. 只有在 Qwen 音色不能满足要求时，再用有授权的样本测试豆包声音复刻 2.0。
6. 所有 PoC 都通过前，不修改正式业务链路，也不删除现有 ARTC 预设变声回退。

## 8. 尚未确认

- 独立 Qwen-Audio-TTS 是否对当前账号开放 `longanqian_v3.1`。
- 本地 ASR 具体采用哪个模型、运行时和硬件。
- 两条 ASR 路线的真实准确率、首包延迟和资源占用。
- 豆包复刻音色在实际客服语料上的相似度和稳定性。
- 任一 ASR + TTS 链路能否达到业务可接受的句级延迟。
