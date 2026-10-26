from __future__ import annotations

import queue
import subprocess
import tempfile
import threading
import time
from collections import deque
from collections.abc import Sequence
from pathlib import Path
from typing import Any


VOICE_MODES = {
    0: "关闭音色改变",
    1: "老人",
    2: "男孩",
    3: "女孩",
    4: "机器人",
    5: "大魔王",
    6: "KTV",
    7: "回声",
    8: "方言",
    9: "怒吼",
    10: "电音",
    11: "留声机",
    100: "自定义组合音色（可调女声）",
    101: "接近 longanqian（原生女声）",
    102: "接近 longanqian（男声转女声）",
}
VOICE_MODE_SDK_NAMES = {
    0: "AliEngineAudioEffectVoiceChangerOff",
    1: "AliEngineAudioEffectVoiceChangerOldMan",
    2: "AliEngineAudioEffectVoiceChangerBabyBoy",
    3: "AliEngineAudioEffectVoiceChangerBabGirl",
    4: "AliEngineAudioEffectVoiceChangerRobot",
    5: "AliEngineAudioEffectVoiceChangerDaimo",
    6: "AliEngineAudioEffectVoiceChangerKTV",
    7: "AliEngineAudioEffectVoiceChangerEcho",
    8: "AliEngineAudioEffectVoiceChangerDialect",
    9: "AliEngineAudioEffectVoiceChangerHow",
    10: "AliEngineAudioEffectVoiceChangerElectroinc",
    11: "AliEngineAudioEffectVoiceChangerPhonograph",
    100: "AliRTC custom effect combination",
    101: "AliRTC preset approximation: longanqian natural female",
    102: "AliRTC preset approximation: longanqian male-to-female",
}
REVERB_MODES = {
    0: "关闭混响",
    1: "人声 I",
    2: "人声 II",
    3: "澡堂",
    4: "明亮小房间",
    5: "黑暗小房间",
    6: "中等房间",
    7: "大房间",
    8: "教堂走廊",
}
BEAUTIFY_MODES = {
    0: "关闭美声",
    1: "浑厚",
    2: "嘹亮",
}
COMBO_VOICE_DEFAULTS = {
    "baseVoiceMode": 3,
    "pitch": 1.10,
    "reverbMode": 1,
    "beautifyMode": 2,
    "lowGain": -1.0,
    "midGain": 2.0,
    "highGain": 3.0,
}
COMBO_VOICE_PRESETS = {
    101: {
        "baseVoiceMode": 0,
        "pitch": 1.01,
        "reverbMode": 0,
        "beautifyMode": 2,
        "lowGain": -2.0,
        "midGain": 2.0,
        "highGain": 1.0,
    },
    102: {
        "baseVoiceMode": 3,
        "pitch": 1.12,
        "reverbMode": 0,
        "beautifyMode": 2,
        "lowGain": -3.0,
        "midGain": 2.0,
        "highGain": 2.0,
    },
}
HEARTBEAT_TIMEOUT_SECONDS = 30.0
DEFAULT_HELPER_PATH = (
    Path(__file__).resolve().parent / "native" / "bin" / "rtc_native_audio.exe"
)


class NativeAudioError(RuntimeError):
    pass


def _number_in_range(
    value: object,
    *,
    label: str,
    minimum: float,
    maximum: float,
    integer: bool = False,
) -> int | float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} 必须是数字")
    if value < minimum or value > maximum:
        raise ValueError(f"{label} 必须在 {minimum}-{maximum} 之间")
    return int(value) if integer else round(float(value), 2)


def normalize_combo_voice(value: object) -> dict[str, int | float]:
    if not isinstance(value, dict):
        raise ValueError("组合音色参数必须是对象")
    return {
        "baseVoiceMode": _number_in_range(
            value.get("baseVoiceMode"),
            label="基础音色",
            minimum=0,
            maximum=11,
            integer=True,
        ),
        "pitch": _number_in_range(
            value.get("pitch"),
            label="音调",
            minimum=0.5,
            maximum=2.0,
        ),
        "reverbMode": _number_in_range(
            value.get("reverbMode"),
            label="混响",
            minimum=0,
            maximum=8,
            integer=True,
        ),
        "beautifyMode": _number_in_range(
            value.get("beautifyMode"),
            label="美声",
            minimum=0,
            maximum=2,
            integer=True,
        ),
        "lowGain": _number_in_range(
            value.get("lowGain"),
            label="低频增益",
            minimum=-15,
            maximum=15,
        ),
        "midGain": _number_in_range(
            value.get("midGain"),
            label="中频增益",
            minimum=-15,
            maximum=15,
        ),
        "highGain": _number_in_range(
            value.get("highGain"),
            label="高频增益",
            minimum=-15,
            maximum=15,
        ),
    }


def voice_settings_for_mode(
    voice_mode: int,
    combo_voice: object | None,
) -> dict[str, int | float] | None:
    if voice_mode == 100:
        return normalize_combo_voice(combo_voice)
    if voice_mode in COMBO_VOICE_PRESETS:
        return dict(COMBO_VOICE_PRESETS[voice_mode])
    return None


class NativeAudioManager:
    def __init__(
        self,
        helper_path: Path | None = None,
        *,
        command: Sequence[str] | None = None,
        heartbeat_timeout_seconds: float = HEARTBEAT_TIMEOUT_SECONDS,
    ) -> None:
        self.helper_path = Path(helper_path or DEFAULT_HELPER_PATH).resolve()
        self.command = list(command) if command else [str(self.helper_path)]
        self.heartbeat_timeout_seconds = heartbeat_timeout_seconds
        self._command_lock = threading.RLock()
        self._state_lock = threading.Lock()
        self._events: queue.Queue[tuple[str, list[str]]] = queue.Queue()
        self._process: subprocess.Popen[str] | None = None
        self._reader_thread: threading.Thread | None = None
        self._stderr_thread: threading.Thread | None = None
        self._watchdog_thread: threading.Thread | None = None
        self._last_heartbeat = 0.0
        self._stderr_lines: deque[str] = deque(maxlen=12)
        self._working_directory = (
            Path(tempfile.gettempdir()) / "rtc-native-audio"
        )
        self._status: dict[str, Any] = {
            "running": False,
            "joined": False,
            "microphoneEnabled": False,
            "voiceMode": 0,
            "comboVoice": dict(COMBO_VOICE_DEFAULTS),
            "userId": "",
            "sessionId": "",
            "lastError": "",
        }

    @property
    def available(self) -> bool:
        return bool(self.command) and Path(self.command[0]).is_file()

    def status(self) -> dict[str, Any]:
        with self._state_lock:
            return {
                **self._status,
                "available": self.available,
                "voiceModeLabel": VOICE_MODES.get(
                    int(self._status["voiceMode"]),
                    str(self._status["voiceMode"]),
                ),
                "diagnostics": list(self._stderr_lines),
            }

    def _set_status(self, **values: Any) -> None:
        with self._state_lock:
            self._status.update(values)

    def _reset_status(self) -> None:
        self._set_status(
            running=False,
            joined=False,
            microphoneEnabled=False,
            voiceMode=0,
            comboVoice=dict(COMBO_VOICE_DEFAULTS),
            userId="",
            sessionId="",
            lastError="",
        )

    def _read_stdout(self, process: subprocess.Popen[str]) -> None:
        assert process.stdout is not None
        try:
            for raw_line in process.stdout:
                line = raw_line.strip()
                if not line:
                    continue
                fields = line.split("\t")
                event_type = fields[0]
                self._events.put((event_type, fields[1:]))
                if event_type == "JOINED":
                    result = int(fields[1]) if len(fields) > 1 else 1
                    user_id = fields[3] if len(fields) > 3 else ""
                    self._set_status(
                        joined=result == 0,
                        userId=user_id if result == 0 else "",
                    )
                elif event_type == "MIC_RESULT" and len(fields) >= 3:
                    result = int(fields[1])
                    enabled = fields[2] == "1"
                    self._set_status(
                        microphoneEnabled=enabled if result == 0 else False,
                    )
                elif event_type == "VOICE_RESULT" and len(fields) >= 3:
                    if int(fields[1]) == 0:
                        self._set_status(voiceMode=int(fields[2]))
                elif event_type in {"ERROR", "BYE"}:
                    self._set_status(lastError=f"{event_type}: {line}")
                elif event_type in {"FATAL", "COMMAND_ERROR"}:
                    self._set_status(lastError=line)
        finally:
            return_code = process.poll()
            if return_code is None:
                try:
                    return_code = process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    return_code = None
            self._set_status(
                running=False,
                joined=False,
                microphoneEnabled=False,
            )
            if return_code not in {None, 0}:
                self._set_status(lastError=f"helper exited: {return_code}")

    def _read_stderr(self, process: subprocess.Popen[str]) -> None:
        assert process.stderr is not None
        for raw_line in process.stderr:
            line = raw_line.strip()
            if line:
                self._stderr_lines.append(line)

    def _wait_event(
        self,
        expected: str,
        *,
        timeout_seconds: float = 15.0,
    ) -> list[str]:
        deadline = time.monotonic() + timeout_seconds
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise NativeAudioError(f"等待音频助手事件 {expected} 超时")
            try:
                event_type, fields = self._events.get(timeout=remaining)
            except queue.Empty as exc:
                raise NativeAudioError(
                    f"等待音频助手事件 {expected} 超时"
                ) from exc
            if event_type == "FATAL":
                detail = fields[0] if fields else "unknown"
                raise NativeAudioError(f"音频助手初始化失败：{detail}")
            if event_type == "COMMAND_ERROR":
                detail = fields[0] if fields else "unknown"
                raise NativeAudioError(f"音频助手拒绝命令：{detail}")
            if event_type == "JOIN_REQUEST" and expected == "JOINED":
                result = int(fields[0]) if fields else 1
                if result != 0:
                    raise NativeAudioError(
                        f"Windows ARTC 入会请求失败：result={result}"
                    )
            if event_type == expected:
                return fields
            if event_type == "JOINED" and expected == "JOINED":
                return fields

    def _send(self, *fields: object) -> None:
        process = self._process
        if process is None or process.stdin is None:
            raise NativeAudioError("音频助手未运行")
        if process.poll() is not None:
            raise NativeAudioError("音频助手进程已经退出")
        process.stdin.write("\t".join(str(field) for field in fields) + "\n")
        process.stdin.flush()

    def _watchdog_loop(
        self,
        process: subprocess.Popen[str],
    ) -> None:
        while True:
            time.sleep(1.0)
            with self._command_lock:
                if self._process is not process:
                    return
                if process.poll() is not None:
                    return
                if (
                    time.monotonic() - self._last_heartbeat
                    <= self.heartbeat_timeout_seconds
                ):
                    continue
                self._stop_locked("heartbeat_timeout")
                return

    def start(
        self,
        auth: dict[str, Any],
        *,
        session_id: str,
        voice_mode: int,
        combo_voice: object | None = None,
    ) -> dict[str, Any]:
        if not self.available:
            raise NativeAudioError(
                f"Windows ARTC 音频助手不存在：{self.helper_path}"
            )
        token = str(auth.get("token", "")).strip()
        if not token:
            raise NativeAudioError("ARTC 麦克风 Token 不能为空")
        if voice_mode not in VOICE_MODES:
            raise ValueError("不支持的音色改变模式")
        normalized_combo = voice_settings_for_mode(voice_mode, combo_voice)

        with self._command_lock:
            if self._process is not None and self._process.poll() is None:
                self._stop_locked("restart")
            while not self._events.empty():
                self._events.get_nowait()
            self._stderr_lines.clear()
            self._reset_status()
            self._set_status(
                sessionId=session_id,
                voiceMode=voice_mode,
                comboVoice=normalized_combo or dict(COMBO_VOICE_DEFAULTS),
            )
            self._last_heartbeat = time.monotonic()
            self._working_directory.mkdir(parents=True, exist_ok=True)

            try:
                self._process = subprocess.Popen(
                    self.command,
                    cwd=str(self._working_directory),
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    bufsize=1,
                )
                self._set_status(running=True)
                self._reader_thread = threading.Thread(
                    target=self._read_stdout,
                    args=(self._process,),
                    daemon=True,
                )
                self._stderr_thread = threading.Thread(
                    target=self._read_stderr,
                    args=(self._process,),
                    daemon=True,
                )
                self._reader_thread.start()
                self._stderr_thread.start()

                self._wait_event("READY", timeout_seconds=10.0)
                if voice_mode >= 100:
                    assert normalized_combo is not None
                    self._send(
                        "COMBO",
                        normalized_combo["baseVoiceMode"],
                        f"{normalized_combo['pitch']:.2f}",
                        normalized_combo["reverbMode"],
                        normalized_combo["beautifyMode"],
                        f"{normalized_combo['lowGain']:.2f}",
                        f"{normalized_combo['midGain']:.2f}",
                        f"{normalized_combo['highGain']:.2f}",
                    )
                    voice_result = self._wait_event("COMBO_RESULT")
                else:
                    self._send("VOICE", voice_mode)
                    voice_result = self._wait_event("VOICE_RESULT")
                if int(voice_result[0]) != 0:
                    raise NativeAudioError(
                        f"音色设置失败：SDK result={voice_result[0]}"
                    )

                self._send("JOIN", token)
                joined = self._wait_event("JOINED", timeout_seconds=20.0)
                result = int(joined[0]) if joined else 1
                if result != 0:
                    raise NativeAudioError(f"Windows ARTC 入会失败：result={result}")
                user_id = joined[2] if len(joined) > 2 else ""
                self._set_status(
                    running=True,
                    joined=True,
                    userId=user_id,
                    voiceMode=voice_mode,
                )
                self._watchdog_thread = threading.Thread(
                    target=self._watchdog_loop,
                    args=(self._process,),
                    daemon=True,
                )
                self._watchdog_thread.start()
                return self.status()
            except Exception:
                self._stop_locked("start_failed")
                raise

    def set_microphone(self, enabled: bool) -> dict[str, Any]:
        with self._command_lock:
            status = self.status()
            if not status["running"] or not status["joined"]:
                raise NativeAudioError("Windows ARTC 音频助手尚未入会")
            self._send("MIC", 1 if enabled else 0)
            result = self._wait_event("MIC_RESULT")
            if int(result[0]) != 0:
                raise NativeAudioError(
                    f"客服麦克风状态切换失败：SDK result={result[0]}"
                )
            acknowledged = len(result) > 1 and result[1] == "1"
            self._set_status(microphoneEnabled=acknowledged)
            return self.status()

    def set_voice_mode(
        self,
        voice_mode: int,
        combo_voice: object | None = None,
    ) -> dict[str, Any]:
        if voice_mode not in VOICE_MODES:
            raise ValueError("不支持的音色改变模式")
        normalized_combo = voice_settings_for_mode(voice_mode, combo_voice)
        with self._command_lock:
            status = self.status()
            if not status["running"] or not status["joined"]:
                raise NativeAudioError("Windows ARTC 音频助手尚未入会")
            if voice_mode >= 100:
                assert normalized_combo is not None
                self._send(
                    "COMBO",
                    normalized_combo["baseVoiceMode"],
                    f"{normalized_combo['pitch']:.2f}",
                    normalized_combo["reverbMode"],
                    normalized_combo["beautifyMode"],
                    f"{normalized_combo['lowGain']:.2f}",
                    f"{normalized_combo['midGain']:.2f}",
                    f"{normalized_combo['highGain']:.2f}",
                )
                result = self._wait_event("COMBO_RESULT")
            else:
                self._send("VOICE", voice_mode)
                result = self._wait_event("VOICE_RESULT")
            if int(result[0]) != 0:
                raise NativeAudioError(
                    f"音色改变模式切换失败：SDK result={result[0]}"
                )
            self._set_status(
                voiceMode=voice_mode,
                comboVoice=(
                    normalized_combo
                    if normalized_combo is not None
                    else dict(COMBO_VOICE_DEFAULTS)
                ),
            )
            return self.status()

    def refresh_token(self, token: str) -> dict[str, Any]:
        token = token.strip()
        if not token:
            raise NativeAudioError("ARTC 麦克风 Token 不能为空")
        with self._command_lock:
            status = self.status()
            if not status["running"] or not status["joined"]:
                raise NativeAudioError("Windows ARTC 音频助手尚未入会")
            self._send("TOKEN", token)
            result = self._wait_event("TOKEN_RESULT")
            if int(result[0]) != 0:
                raise NativeAudioError(f"Token 刷新失败：SDK result={result[0]}")
            return self.status()

    def heartbeat(self) -> dict[str, Any]:
        with self._command_lock:
            status = self.status()
            if not status["running"]:
                raise NativeAudioError("Windows ARTC 音频助手未运行")
            self._last_heartbeat = time.monotonic()
            return status

    def _stop_locked(self, reason: str) -> None:
        process = self._process
        if process is None:
            return
        if process.poll() is None and process.stdin is not None:
            try:
                self._send("STOP")
                self._wait_event("STOPPED", timeout_seconds=5.0)
            except (NativeAudioError, OSError):
                pass
        if process.poll() is None:
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=2)
        for thread in (self._reader_thread, self._stderr_thread):
            if thread is not None:
                thread.join(timeout=1)
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream is not None and not stream.closed:
                stream.close()
        self._process = None
        self._set_status(
            running=False,
            joined=False,
            microphoneEnabled=False,
            lastError=(
                self.status()["lastError"]
                if reason == "start_failed"
                else ""
            ),
        )

    def stop(self) -> dict[str, Any]:
        with self._command_lock:
            self._stop_locked("stop")
            return self.status()
