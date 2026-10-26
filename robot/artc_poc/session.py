from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Protocol

from .audio import PcmFrame, PcmOutput, s16le_peak


class EngineEventSink(Protocol):
    def on_join_result(self, result: int, channel: str, user_id: str) -> None:
        ...

    def on_leave_result(self, result: int) -> None:
        ...

    def on_error(self, code: str, message: str) -> None:
        ...

    def on_remote_user_online(self, user_id: str) -> None:
        ...

    def on_remote_user_offline(self, user_id: str) -> None:
        ...

    def on_remote_audio(self, frame: PcmFrame) -> None:
        ...


class RobotEngine(Protocol):
    def join(
        self,
        token: str,
        channel: str,
        user_id: str,
        user_name: str,
        *,
        video_enabled: bool = False,
    ) -> int:
        ...

    def set_microphone_enabled(self, enabled: bool) -> int:
        ...

    def leave(self) -> int:
        ...

    def release(self) -> int:
        ...


class PoCSessionError(RuntimeError):
    pass


FATAL_ENGINE_ERROR_CODES = {
    "ERR_FATAL",
    "ERR_NETWORK_CONNECT_FAIL",
    "ERR_NETWORK_DISCONNECT",
    "ERR_NETWORK_RECONNECT_FAIL",
    "ERR_NETWORK_TIMEOUT",
    "ERR_SDK_INVALID_STATE",
    "ERR_SESSION_REMOVED",
}


class RobotPoCSession:
    def __init__(self, audio_output: PcmOutput) -> None:
        self.audio_output = audio_output
        self.engine: RobotEngine | None = None
        self.state = "idle"
        self.microphone_enabled = False
        self.video_enabled = False
        self.channel = ""
        self.user_id = ""
        self.remote_users: set[str] = set()
        self.error_message = ""
        self.last_error = ""
        self.remote_audio_frames = 0
        self.remote_audio_bytes = 0
        self.remote_audio_peak = 0
        self._join_finished = threading.Event()
        self._leave_finished = threading.Event()

    @property
    def join_finished(self) -> threading.Event:
        return self._join_finished

    @property
    def leave_finished(self) -> threading.Event:
        return self._leave_finished

    def attach_engine(self, engine: RobotEngine) -> None:
        if self.engine is not None:
            raise PoCSessionError("Robot Engine 已经绑定")
        self.engine = engine

    def join(
        self,
        token: str,
        channel: str,
        user_id: str,
        user_name: str,
        *,
        video_enabled: bool = False,
    ) -> None:
        if self.engine is None:
            raise PoCSessionError("Robot Engine 尚未绑定")
        if self.state != "idle":
            raise PoCSessionError(f"当前状态 {self.state} 不能加入频道")
        if not token:
            raise PoCSessionError("ARTC Token 不能为空")

        self.channel = channel
        self.user_id = user_id
        self.video_enabled = video_enabled
        self.state = "joining"
        self._join_finished.clear()
        self._leave_finished.clear()

        mic_result = self.engine.set_microphone_enabled(False)
        if mic_result != 0:
            self.state = "error"
            raise PoCSessionError(f"加入频道前关闭麦克风失败：{mic_result}")

        result = self.engine.join(
            token,
            channel,
            user_id,
            user_name,
            video_enabled=video_enabled,
        )
        if result != 0:
            self.state = "error"
            raise PoCSessionError(f"调用 ARTC JoinChannel 失败：{result}")

    def set_microphone_enabled(self, enabled: bool) -> None:
        if self.engine is None:
            raise PoCSessionError("Robot Engine 尚未绑定")
        if self.state != "joined":
            raise PoCSessionError(f"当前状态 {self.state} 不能切换麦克风")
        result = self.engine.set_microphone_enabled(enabled)
        if result != 0:
            raise PoCSessionError(f"切换机器人麦克风失败：{result}")
        self.microphone_enabled = enabled

    def set_remote_playback_volume(self, volume: int) -> None:
        self.audio_output.set_volume(volume)

    def on_join_result(self, result: int, channel: str, user_id: str) -> None:
        if result == 0:
            self.state = "joined"
        else:
            self.state = "error"
            self.error_message = (
                f"加入 ARTC 频道失败：result={result}, channel={channel}, "
                f"user={user_id}"
            )
        self._join_finished.set()

    def on_leave_result(self, result: int) -> None:
        if result != 0:
            self.state = "error"
            if not self.error_message:
                self.error_message = (
                    "ARTC 在入会/会中阶段异常结束："
                    f"result={result}"
                )
            if not self._join_finished.is_set():
                self._join_finished.set()
        self._leave_finished.set()

    def on_error(self, code: str, message: str) -> None:
        self.last_error = f"{code}: {message}"
        if code in FATAL_ENGINE_ERROR_CODES:
            self.state = "error"
            self.error_message = self.last_error
            self._join_finished.set()

    def on_remote_user_online(self, user_id: str) -> None:
        self.remote_users.add(user_id)

    def on_remote_user_offline(self, user_id: str) -> None:
        self.remote_users.discard(user_id)

    def on_remote_audio(self, frame: PcmFrame) -> None:
        if not frame.data:
            return
        self.remote_audio_frames += 1
        self.remote_audio_bytes += len(frame.data)
        self.remote_audio_peak = max(
            self.remote_audio_peak,
            s16le_peak(frame.data),
        )
        self.audio_output.feed(frame)

    def close(
        self,
        pump_events: Callable[[float], None],
        *,
        timeout_seconds: float = 5,
    ) -> None:
        if self.engine is None or self.state in {"idle", "stopped"}:
            self.state = "stopped"
            self.audio_output.close()
            return

        self.state = "stopping"
        try:
            if self.microphone_enabled:
                result = self.engine.set_microphone_enabled(False)
                if result != 0:
                    self.error_message = f"停止机器人麦克风失败：{result}"
                else:
                    self.microphone_enabled = False
        except Exception as exc:
            self.error_message = f"停止机器人麦克风失败：{exc}"

        try:
            result = self.engine.leave()
            if result != 0:
                self.error_message = f"调用 LeaveChannel 失败：{result}"
        except Exception as exc:
            self.error_message = f"调用 LeaveChannel 异常：{exc}"

        if not self._leave_finished.is_set():
            self._wait_for_event(
                self._leave_finished,
                pump_events,
                timeout_seconds,
            )

        try:
            result = self.engine.release()
            if result != 0:
                self.error_message = f"释放 ARTC Engine 失败：{result}"
        except Exception as exc:
            self.error_message = f"释放 ARTC Engine 失败：{exc}"
        finally:
            self.audio_output.close()
            self.state = "stopped"

    @staticmethod
    def _wait_for_event(
        event: threading.Event,
        pump_events: Callable[[float], None],
        timeout_seconds: float,
    ) -> bool:
        import time

        deadline = time.monotonic() + timeout_seconds
        while not event.is_set() and time.monotonic() < deadline:
            pump_events(0.05)
        return event.is_set()
