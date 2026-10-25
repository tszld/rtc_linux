# ARTC Linux 机器人 + Web 客服联调

更新日期：2026-10-23

本文记录跨 SDK 的本地联调方式：机器人使用阿里云 ARTC Linux Python SDK，客服使用
浏览器 ARTC Web SDK。业务后端负责登录、会话、短期 Token、WebSocket 和人工接管锁，
不转发媒体。

## 已验证版本

- Linux ARTC SDK：7.11.0
- Linux 包：`AliRTCSDK_Linux-7.11.0.tar.gz`
- Linux 包 SHA-256：
  `308dd485bc2d0bcab153be4232f38b568a4f92b30538e763c3f069058924172b`
- `AliRtcCoreService` SHA-256：
  `d0f81ebb14a36c89598b15c794a14c167869317a5928cfd0f42e45dcfe689e01`
- `libAliRtcLinuxEngine.so` SHA-256：
  `16e51638ee95d1aa1b554b8911b69438dac50396a5a9f2dbf0c4a6dbaa932470`
- ARTC Web SDK：7.3.4
- Web SDK SHA-256：
  `46b57790f4539150f45c6ab3f113e9ed0b73e756fd3c97a0d6813c99628ba6d1`

Linux SDK 二进制和真实凭证不进入版本库：

```text
robot/vendor/
config.json
*.token
```

## 环境准备

控制端和机器人端使用 Linux x86_64、Python 3.10.x。客服 Web 服务可使用 Windows：

```bash
conda activate rtc
python --version
```

下载并解压官方 Linux SDK 到被忽略的本地目录：

```bash
mkdir -p robot/vendor
curl -fL \
  'https://alivc-demo-cms.alicdn.com/versionProduct/sdk/linux/AliRTCSDK_Linux-7.11.0.tar.gz' \
  -o /tmp/AliRTCSDK_Linux-7.11.0.tar.gz
sha256sum /tmp/AliRTCSDK_Linux-7.11.0.tar.gz
tar -xzf /tmp/AliRTCSDK_Linux-7.11.0.tar.gz -C robot/vendor
```

`sha256sum` 必须与本文记录一致。

## 统一配置

从 `config.example.json` 创建未跟踪的 `config.json`，在 `server` 节填写 AppID 和
AppKey，在 `cluster.control_url` 填写控制端 Tailscale 地址。项目不读取环境变量或
组件目录下的旧配置文件。

安装服务端依赖并启动：

```bash
python -m pip install -r requirements-server.txt
python main.py server --create-admin admin
python main.py server
```

机器人 ID 和设备凭证填写到 `config.json` 的 `robot` 节。启动机器人后会自动注册为
待审批设备，管理员在 Windows 管理页 `/admin.html` 审批。业务场景使用隐藏的默认
场景，不需要管理员操作。

## 启动机器人

```bash
python -m pip install -r requirements-robot.txt
python main.py robot
```

机器人会：

- 使用设备凭证登录业务后端。
- 维持 WebSocket 和在线心跳。
- 开始一次可见业务会话。
- 使用后端签发的短期 Token 加入独立 ARTC 频道。
- 默认关闭麦克风和摄像头。
- 业务会话建立后默认发布机器人麦克风；人工接管期间保持双向音频。
- 在人工接管期间暂停自动业务语音并播放客服远端音频。

## 启动 Web 客服

启动 Web 客服入口：

```bash
python main.py agent
```

该命令会打开默认浏览器；无图形环境可使用 `python main.py agent --no-browser` 输出
地址。客服 Web 默认地址为：

```text
http://127.0.0.1:8100/
```

客服 Web 配置位于根目录 `config.json` 的 `agent` 节。Windows 浏览器和客服 Web 服务
同机时，`host` 保持 `127.0.0.1`，连接地址来自 `cluster.control_url`。客服页面来源
为 localhost 时无需额外 CORS；如果从其他主机访问客服页面，再在 `server.cors_origins`
中加入来源。

客服登录后：

1. 选择有权限的业务会话。
2. 点击“开始处理”，由后端独占认领并接入；已被其他客服认领的会话不能接入。
3. 确认能听到机器人音频。
4. 点击“开始接管”，取得租约后再点击“开启麦克风”。
5. 确认机器人扬声器播放客服声音。
6. 点击“释放接管”，确认客服麦克风关闭且自动语音暂停标记被清除。
7. 点击“离开会话”只释放占用，确认会话重新显示“等待客服接入”。
8. 点击“结束会话”，确认机器人停止媒体并退出进程；下次启动复用已有设备身份。

客服音频和摄像头在入会前均关闭。没有有效接管租约时，页面不会调用麦克风发布。
客服接管后准备讲话时，先点击“开启麦克风”。机器人端必须配置
`robot.audio_output=true` 且安装 `ffplay`，否则客服端虽然显示“发布中”，机器人会
统计远端音频但不输出到扬声器。该配置默认启用，无扬声器环境才显式设为 `false`。

## 验收流程

1. Linux 机器人上线并被 Web 客服看到。
2. Web 客服收到机器人业务语音。
3. 客服申请人工接管锁并发布麦克风，机器人扬声器播放。
4. 接管期间设置自动语音暂停标记；项目本身不包含 TTS，外部业务模块需消费该标记。
5. 机器人默认发布麦克风，Web 客服能够收到机器人侧语音。
6. 第二个客服看到会话被占用，不能同时接入或接管。
7. 关闭浏览器或控制面断线后，租约和监听状态被后端清理。
8. 等待接近 Token 过期时间，确认 Web SDK 刷新后媒体不中断。
9. 如果启用摄像头，确认 Web 客服显示机器人相机画面。
10. 长时间运行后确认无媒体泄漏和服务进程残留。

## 自动化验证

```bash
python -m unittest discover -s tests -v
python -m compileall -q server robot agent tools tests
python -m tools.secret_scan
```

自动化测试不访问真实 ARTC 云，覆盖：

- 登录、权限、场景隔离、心跳、会话和并发接管锁。
- ARTC Token 固定向量和 AppKey 不进入客户端。
- 机器人空闲时关闭麦克风，活动业务会话中默认发布并执行媒体清理。
- 独立客服静态服务、跨域授权、官方 SDK 文件哈希和入会前关闭发布。

## 当前状态

- 原两个 Linux Python 客户端之间的合成音频链路曾通过真实 ARTC 验证。
- 原 Linux Python 客服端已移除。
- Linux 机器人 + Web 客服跨 SDK 真实音频闭环尚未在本文更新时完成验收。
- 物理麦克风、扬声器听感、摄像头、网络重连和长时间运行仍待真实设备验证。

## 安全边界

- AppKey 只存在后端进程或未跟踪配置。
- 浏览器只接收短期 ARTC Token，不接收 AppKey。
- 页面不把访问 Token 或 ARTC Token 写入本地持久化存储。
- 不保存或录制音视频。
- 机器人、客服麦克风和人工接管状态必须可见，支持明确停止。
