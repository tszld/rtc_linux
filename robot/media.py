from __future__ import annotations

import asyncio
from typing import Any

from .artc_poc.audio import create_pcm_output
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
        self.session_id: str | None = None

    @property
    def is_active(self) -> bool:
        return self.session is not None and self.session.state == "joined"

    @property
    def microphone_enabled(self) -> bool:
        return bool(self.session and self.session.microphone_enabled)

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
        try:
            session.join(
                auth["token"],
                auth["channel"],
                auth["userId"],
                auth["userName"],
            )
            wait_for_join(session, 20)
            result = engine.enable_external_audio_source(16000, 1)
            if result != 0:
                raise RuntimeError(
                    f"Failed to enable external audio source: {result}"
                )
        except Exception:
            session.close(pump_events)
            raise
        self.session = session
        self.session_id = session_id

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
            pump_events(0.02)

    def state(self, *, automatic_speech_paused: bool) -> dict[str, Any]:
        return {
            "sessionId": self.session_id,
            "microphonePublished": self.microphone_enabled,
            "cameraPublished": False,
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
        if self.audio_publisher is not None:
            self.audio_publisher.stop()
            self.audio_publisher = None
        if self.session is not None:
            self.session.close(pump_events)
            self.session = None
        self.session_id = None
