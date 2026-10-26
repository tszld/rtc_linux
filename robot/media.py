from __future__ import annotations

import asyncio
from typing import Any

from .artc_poc.audio import create_pcm_output
from .artc_poc.camera import CameraPublisher
from .artc_poc.engine import OfficialArtcEngine, resolve_official_sdk_paths
from .artc_poc.main import pump_events, set_microphone_enabled, wait_for_join
from .artc_poc.microphone import MicrophonePublisher
from .artc_poc.session import RobotPoCSession
from .artc_poc.tone import TonePublisher
from .config import RobotConfig


class RobotMediaController:
    def __init__(self, config: RobotConfig) -> None:
        self.config = config
        self.session: RobotPoCSession | None = None
        self.audio_publisher: TonePublisher | MicrophonePublisher | None = None
        self.camera_publisher: CameraPublisher | None = None
        self.session_id: str | None = None
        self.agent_playback_volume = 100

    @property
    def is_active(self) -> bool:
        return self.session is not None and self.session.state == "joined"

    @property
    def microphone_enabled(self) -> bool:
        return bool(self.session and self.session.microphone_enabled)

    @property
    def camera_enabled(self) -> bool:
        return bool(self.camera_publisher and self.camera_publisher.is_running)

    def start(self, auth: dict[str, Any], session_id: str) -> None:
        if self.session is not None:
            if self.session_id == session_id:
                return
            raise RuntimeError("已有 ARTC 媒体会话")
        paths = resolve_official_sdk_paths(self.config.sdk_root)
        output = create_pcm_output(self.config.audio_output)
        session = RobotPoCSession(output)
        engine = OfficialArtcEngine.create(
            paths,
            session,
            log_dir=self.config.log_dir,
        )
        if self.config.test_tone_frequency is not None:
            self.audio_publisher = TonePublisher(
                engine,
                sample_rate=16000,
                frequency_hz=self.config.test_tone_frequency,
            )
        else:
            self.audio_publisher = MicrophonePublisher(
                engine,
                sample_rate=16000,
                channels=1,
        )
        session.attach_engine(engine)
        camera_publisher: CameraPublisher | None = None
        try:
            if self.config.camera_enabled:
                camera_publisher = CameraPublisher(
                    engine,
                    device=self.config.camera_device,
                )
                camera_publisher.start()
            session.join(
                auth["token"],
                auth["channel"],
                auth["userId"],
                auth["userName"],
                video_enabled=self.config.camera_enabled,
            )
            wait_for_join(session, 20)
            result = engine.enable_external_audio_source(16000, 1)
            if result != 0:
                raise RuntimeError(
                    f"Failed to enable external audio source: {result}"
                )
            if camera_publisher is not None:
                camera_publisher.prime(2)
                print(
                    "机器人摄像头已发布："
                    f"device={self.config.camera_device} "
                    f"frames={camera_publisher.pushed_frames}",
                    flush=True,
                )
        except Exception:
            if camera_publisher is not None:
                camera_publisher.stop()
            session.close(pump_events)
            raise
        self.session = session
        self.camera_publisher = camera_publisher
        self.session_id = session_id
        session.set_remote_playback_volume(self.agent_playback_volume)

    def set_microphone(self, enabled: bool) -> None:
        if self.session is None:
            if enabled:
                raise RuntimeError("媒体会话未启动，不能开麦")
            return
        set_microphone_enabled(
            self.session,
            self.audio_publisher,
            enabled,
        )

    def pump(self) -> None:
        if self.session is not None:
            if self.audio_publisher is not None:
                self.audio_publisher.pump()
            if self.camera_publisher is not None:
                self.camera_publisher.pump()
            pump_events(0.02)

    def set_agent_playback_volume(self, volume: int) -> None:
        if not isinstance(volume, int) or volume < 0 or volume > 100:
            raise ValueError("客服播放音量必须是 0-100 的整数")
        self.agent_playback_volume = volume
        if self.session is not None:
            self.session.set_remote_playback_volume(volume)

    def state(self, *, automatic_speech_paused: bool) -> dict[str, Any]:
        return {
            "sessionId": self.session_id,
            "microphonePublished": self.microphone_enabled,
            "cameraPublished": self.camera_enabled,
            "automaticSpeechPaused": automatic_speech_paused,
        }

    def metrics(self) -> dict[str, int]:
        if self.session is None:
            return {
                "remoteAudioFrames": 0,
                "remoteAudioBytes": 0,
                "remoteAudioPeak": 0,
            }
        return {
            "remoteAudioFrames": self.session.remote_audio_frames,
            "remoteAudioBytes": self.session.remote_audio_bytes,
            "remoteAudioPeak": self.session.remote_audio_peak,
        }

    def stop(self) -> None:
        if self.camera_publisher is not None:
            self.camera_publisher.stop()
            self.camera_publisher = None
        if self.audio_publisher is not None:
            self.audio_publisher.stop()
            self.audio_publisher = None
        if self.session is not None:
            self.session.close(pump_events)
            self.session = None
        self.session_id = None
