# Windows ARTC 变声音频助手

该助手只负责 Windows 侧客服麦克风：

- 使用独立 ARTC `userId` 加入机器人同一频道。
- 关闭远端音视频订阅，不接管浏览器媒体播放或视频渲染。
- 默认不发布麦克风。
- 调用 Windows ARTC SDK 的 `SetAudioEffectVoiceChangerMode`。
- 通过标准输入接收控制命令，通过标准输出返回状态事件。

浏览器、登录、业务会话、接管锁和机器人音视频仍由现有 Web 工作台处理。

SDK `7.6.0` 的官方音色改变列表为：关闭、老人、男孩、女孩、机器人、大魔王、KTV、
回声、方言、怒吼、电音、留声机。另提供可在线调节的“自定义组合音色”：基础音色、
`0.5-2.0` 音调、9 个混响模式、3 个美声模式和低/中/高三段增益。界面选项和参数由
Python 控制层统一下发。

此外提供两个固定服务端参数预设：

```text
接近 longanqian（原生女声）：关闭基础变声、1.01 音调、关闭混响、嘹亮、
低/中/高 -2/+2/+1 dB

接近 longanqian（男声转女声）：女孩、1.12 音调、关闭混响、嘹亮、
低/中/高 -3/+2/+2 dB
```

## 构建

需要：

- Visual Studio 2022 C++ x64 工具链。
- Windows 10/11 SDK。
- 阿里云 ARTC Windows SDK。当前项目本地目录为
  `agent/native/vendor/AliVCSDK_ARTC-7.6.0`，该目录被 `.gitignore` 忽略。

在项目根目录执行：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass `
  -File agent/native/build.ps1
```

只有把 SDK 放在其他位置时才需要额外传入 `-SdkRoot <path>`。

构建输出位于 Git 跟踪的部署目录：

```text
agent/native/bin/rtc_native_audio.exe
agent/native/bin/AliRTCSdk.dll
agent/native/bin/alivcffmpeg.dll
agent/native/bin/alivcx265.dll
agent/native/bin/PluginAAC.dll
agent/native/bin/x264.dll
agent/native/bin/vc_redist.x64.exe
```

`build/` 是本地 MSVC 中间文件，不提交。`vendor/` 是 SDK 编译输入，也不提交；它们
只用于开发机构建。`bin/` 是 Git 跟踪的预编译运行目录，普通客服电脑直接使用，
不需要安装编译器。开发者重新构建后需要把 `bin/` 的变化一起提交。

`bin/` 只跟踪 EXE、五个 ARTC DLL 和 VC++ 安装程序。ARTC SDK 运行时生成的统计、
日志和缓存文件被忽略；助手进程使用系统临时目录作为工作目录，避免污染项目。

构建脚本会从 Visual Studio 的 Redist 目录复制 `vc_redist.x64.exe`。发布时必须把
EXE、全部 ARTC x64 DLL 和该安装程序一起交付。

目标客服电脑首次部署时仍要运行一次 `vc_redist.x64.exe`；仅仅把安装程序放在
目录中不会自动安装运行库。安装完成后不需要 Visual Studio，也不需要重复安装
ARTC SDK。

## 控制协议

每行一个制表符分隔的命令：

```text
JOIN    <token>
MIC     <0|1>
VOICE   <0-11>
COMBO   <voiceMode> <pitch> <reverb> <beautify> <lowGain> <midGain> <highGain>
TOKEN   <token>
STOP
```

助手返回：

```text
READY
JOIN_REQUEST    <result>
JOINED          <result> <channel> <userId> <elapsed>
MIC_RESULT      <result> <0|1>
VOICE_RESULT    <result> <mode>
COMBO_RESULT    <result> <parameters...> <failedStep>
TOKEN_RESULT    <result>
LEFT            <result> <duration>
STOPPED
FATAL           <reason> [...]
```

Python `agent` 服务负责启动和控制该进程，并限制控制接口只接受 localhost、
同源请求和每次启动生成的本地控制密钥。

## 安全检查

- 助手入会前关闭麦克风发布和远端音视频订阅。
- 浏览器每 5 秒发送本地心跳。
- 本地助手超过 30 秒未收到心跳时自动停止并退出。
- 释放接管、控制连接中断、页面关闭和本地服务退出都会停止助手。
