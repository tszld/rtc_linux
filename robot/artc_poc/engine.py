from __future__ import annotations

import importlib
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType

from .audio import PcmFrame
from .session import EngineEventSink


class OfficialSdkError(RuntimeError):
    pass


@dataclass(frozen=True)
class OfficialSdkPaths:
    python_dir: Path
    core_service_path: Path


def resolve_official_sdk_paths(
    sdk_root: Path,
    core_service_path: Path | None = None,
) -> OfficialSdkPaths:
    root = sdk_root.expanduser().resolve()
    python_dir_candidates = [root, root / "Python"]
    python_dir = next(
        (
            candidate
            for candidate in python_dir_candidates
            if (candidate / "AliRTCEngine.py").is_file()
            and (candidate / "AliRTCLinuxSdkDefine.py").is_file()
        ),
        None,
    )
    if python_dir is None:
        raise OfficialSdkError(
            f"未在 {root} 找到官方 Python SDK；应包含 AliRTCEngine.py"
        )

    core_path = (
        core_service_path.expanduser().resolve()
        if core_service_path is not None
        else python_dir / "Release" / "lib" / "AliRtcCoreService"
    )
    if not core_path.is_file():
        raise OfficialSdkError(f"AliRtcCoreService 不存在：{core_path}")
    return OfficialSdkPaths(python_dir=python_dir, core_service_path=core_path)


def _enum_value(value: object) -> int | str:
    return getattr(value, "value", value)


class OfficialArtcEngine:
    """将官方 7.11.0 Python API 收敛为 PoC 使用的最小适配层。"""

    def __init__(self, engine: object, defines: ModuleType) -> None:
        self._engine = engine
        self._defines = defines

    @classmethod
    def create(
        cls,
        paths: OfficialSdkPaths,
        event_sink: EngineEventSink,
        *,
        log_dir: Path,
    ) -> OfficialArtcEngine:
        python_dir = str(paths.python_dir)
        if python_dir not in sys.path:
            sys.path.insert(0, python_dir)
        try:
            engine_module = importlib.import_module("AliRTCEngine")
            defines = importlib.import_module("AliRTCLinuxSdkDefine")
        except ImportError as exc:
            raise OfficialSdkError(f"导入官方 ARTC Python SDK 失败：{exc}") from exc

        handler = _create_event_handler(event_sink, engine_module, defines)
        log_path = log_dir.expanduser().resolve()
        log_path.mkdir(parents=True, exist_ok=True)
        try:
            engine = engine_module.CreateAliRTCEngine(
                handler,
                42000,
                45000,
                str(log_path),
                str(paths.core_service_path),
                True,
                json.dumps(
                    {"user_specified_disable_audio_ranking": "true"},
                    separators=(",", ":"),
                ),
            )
        except Exception as exc:
            raise OfficialSdkError(f"创建官方 ARTC Engine 失败：{exc}") from exc
        return cls(engine, defines)

    def join(
        self,
        token: str,
        channel: str,
        user_id: str,
        user_name: str,
        *,
        video_enabled: bool = False,
    ) -> int:
        join_config = self._defines.JoinChannelConfig()
        join_config.channelProfile = (
            self._defines.ChannelProfile.ChannelProfileInteractiveLive
        )
        join_config.isAudioOnly = not video_enabled
        join_config.publishMode = self._defines.PublishMode.PublishAutomatically
        join_config.subscribeMode = (
            self._defines.SubscribeMode.SubscribeAutomatically
        )
        join_config.subscribeAudioFormat = (
            self._defines.AudioFormat.AudioFormatMixedPcm
        )
        join_config.publishAvsyncMode = (
            self._defines.PublishAvsyncMode.PublishAvsyncNoDelay
        )

        # 官方 SDK 默认自动开启麦克风和视频；必须在入会前显式设置。
        self._check_result(self._engine.PublishLocalAudioStream(False), "关闭麦克风")
        if video_enabled:
            self._configure_external_video()
        else:
            self._check_result(
                self._engine.PublishLocalVideoStream(False),
                "关闭视频",
            )
        self._check_result(
            self._engine.SetClientRole(
                self._defines.AliEngineClientRole.AliEngineClientRoleInteractive
            ),
            "设置互动角色",
        )
        result = self._engine.JoinChannel(
            token,
            channel,
            user_id,
            user_name,
            join_config,
        )
        return int(result)

    def _configure_external_video(self) -> None:
        encoder_config = self._defines.AliEngineVideoEncoderConfiguration(
            640,
            480,
            self._defines.AliEngineFrameRate.AliEngineFrameRateFps15,
            512,
            (
                self._defines.AliEngineVideoEncoderOrientationMode
                .AliEngineVideoEncoderOrientationModeAdaptive
            ),
            (
                self._defines.AliEngineVideoMirrorMode
                .AliEngineVideoMirrorModeDisabled
            ),
            self._defines.AliEngineRotationMode.AliEngineRotationMode_0,
        )
        self._check_result(
            self._engine.SetVideoEncoderConfiguration(encoder_config),
            "设置视频编码参数",
        )
        self._check_result(
            self._engine.SetExternalVideoSource(
                True,
                self._defines.VideoSource.VideoSourceCamera,
                self._defines.RenderMode.RenderModeFill,
            ),
            "启用外部视频源",
        )
        self._check_result(
            self._engine.PublishLocalVideoStream(True),
            "开启视频推流",
        )

    def set_microphone_enabled(self, enabled: bool) -> int:
        return int(self._engine.PublishLocalAudioStream(bool(enabled)))

    def enable_external_audio_source(
        self,
        sample_rate: int,
        channels: int,
    ) -> int:
        return int(
            self._engine.SetExternalAudioSource(
                True,
                int(sample_rate),
                int(channels),
            )
        )

    def push_external_audio_frame(self, data: bytes, timestamp_ms: int) -> int:
        if len(data) % 2:
            raise ValueError("16-bit PCM 数据长度必须是偶数字节")
        return int(
            self._engine.PushExternalAudioFrameRawData(
                data,
                len(data),
                int(timestamp_ms),
            )
        )

    def push_external_video_frame(
        self,
        data: bytes,
        width: int,
        height: int,
        timestamp_ms: int,
    ) -> int:
        if width <= 0 or height <= 0:
            raise ValueError("Video dimensions must be positive")
        expected_size = width * height * 3
        if len(data) != expected_size:
            raise ValueError(
                f"RGB24 frame length must be {expected_size} bytes"
            )
        sample = self._defines.VideoDataSample()
        sample.width = int(width)
        sample.height = int(height)
        sample.strideY = int(width)
        sample.strideU = int(width)
        sample.strideV = int(width)
        sample.dataLen = expected_size
        sample.format = (
            self._defines.VideoDataFormat.VideoDataFormatRGB24
        )
        sample.bufferType = (
            self._defines.VideoBufferType.VideoBufferTypeRawData
        )
        sample.rotation = 0
        sample.data = data
        sample.timeStamp = int(timestamp_ms)
        return int(
            self._engine.PushExternalVideoFrame(
                sample,
                self._defines.VideoSource.VideoSourceCamera,
            )
        )

    def leave(self) -> int:
        self._check_result(self._engine.PublishLocalAudioStream(False), "关闭麦克风")
        self._check_result(self._engine.PublishLocalVideoStream(False), "关闭视频")
        return int(self._engine.LeaveChannel())

    def release(self) -> int:
        return int(self._engine.Release())

    @staticmethod
    def _check_result(result: int, action: str) -> None:
        if int(result) != 0:
            raise OfficialSdkError(f"{action}失败：result={result}")


def _create_event_handler(
    sink: EngineEventSink,
    engine_module: ModuleType,
    defines: ModuleType,
) -> object:
    base = engine_module.EngineEventHandlerInterface

    class EventHandler(base):
        def OnJoinChannelResult(
            self,
            result: int,
            channel: str,
            userId: str,
        ) -> None:
            sink.on_join_result(int(result), channel, userId)

        def OnLeaveChannelResult(self, result: int) -> None:
            sink.on_leave_result(int(result))

        def OnError(self, error_code: object) -> None:
            error_name = _error_name(error_code, defines)
            sink.on_error(
                error_name,
                f"官方 SDK 错误码 {_enum_value(error_code)}",
            )

        def OnRemoteUserOnLineNotify(self, uid: str) -> None:
            sink.on_remote_user_online(uid)

        def OnRemoteUserOffLineNotify(self, uid: str) -> None:
            sink.on_remote_user_offline(uid)

        def OnSubscribeMixAudioFrame(self, frame: object) -> None:
            pcm = getattr(frame, "pcm", None)
            if pcm is None:
                return
            sink.on_remote_audio(
                PcmFrame(
                    data=bytes(pcm.pcmBuf_),
                    sample_rate=int(pcm.sample_rates_),
                    channels=int(pcm.channels_),
                    sample_bits=int(pcm.sample_bits_),
                )
            )

    return EventHandler()


def _error_name(error_code: object, defines: ModuleType) -> str:
    try:
        return defines.ERROR_CODE(error_code).name
    except (TypeError, ValueError):
        return f"ARTC_ERROR_{_enum_value(error_code)}"
