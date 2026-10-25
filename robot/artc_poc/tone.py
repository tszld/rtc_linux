from __future__ import annotations

import array
import math
import sys
import time
from typing import Protocol


class ExternalAudioEngine(Protocol):
    def push_external_audio_frame(self, data: bytes, timestamp_ms: int) -> int:
        ...


def build_tone_frame(
    sample_rate: int,
    frequency_hz: float,
    *,
    frame_ms: int = 10,
    amplitude: float = 0.2,
    start_sample: int = 0,
) -> bytes:
    if sample_rate <= 0:
        raise ValueError("sample_rate 必须大于 0")
    if frequency_hz <= 0:
        raise ValueError("frequency_hz 必须大于 0")
    if frame_ms <= 0:
        raise ValueError("frame_ms 必须大于 0")
    if not 0 < amplitude <= 1:
        raise ValueError("amplitude 必须在 (0, 1] 范围内")

    sample_count = sample_rate * frame_ms // 1000
    if sample_count <= 0:
        raise ValueError("frame_ms 太短，无法生成音频帧")
    samples = array.array("h")
    for offset in range(sample_count):
        sample_index = start_sample + offset
        value = int(
            32767
            * amplitude
            * math.sin(2 * math.pi * frequency_hz * sample_index / sample_rate)
        )
        samples.append(value)
    if sys.byteorder != "little":
        samples.byteswap()
    return samples.tobytes()


class TonePublisher:
    """测试用外部 PCM 音源；不读取、记录或保存真实麦克风内容。"""

    def __init__(
        self,
        engine: ExternalAudioEngine,
        *,
        sample_rate: int = 16000,
        frequency_hz: float = 440,
        frame_ms: int = 10,
        amplitude: float = 0.2,
    ) -> None:
        self.engine = engine
        self.sample_rate = sample_rate
        self.frequency_hz = frequency_hz
        self.frame_ms = frame_ms
        self.amplitude = amplitude
        self.pushed_frames = 0
        self.push_errors = 0
        self.last_result = 0
        self.last_error = ""
        self._running = False
        self._started_at = 0.0
        self._next_frame = 0

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._started_at = time.monotonic()
        self._next_frame = 0

    def stop(self) -> None:
        self._running = False

    def prime(self, frame_count: int = 5) -> None:
        if not self._running:
            raise RuntimeError("测试音源尚未启动")
        for _ in range(max(0, frame_count)):
            if not self._push_next_frame():
                return

    def pump(self) -> None:
        if not self._running:
            return
        now = time.monotonic()
        while self._running:
            deadline = self._started_at + self._next_frame * self.frame_ms / 1000
            if now < deadline:
                return
            if not self._push_next_frame():
                return
            now = time.monotonic()

    def _push_next_frame(self) -> bool:
        samples_per_frame = self.sample_rate * self.frame_ms // 1000
        timestamp_ms = self._next_frame * self.frame_ms
        try:
            data = build_tone_frame(
                self.sample_rate,
                self.frequency_hz,
                frame_ms=self.frame_ms,
                amplitude=self.amplitude,
                start_sample=self._next_frame * samples_per_frame,
            )
            result = self.engine.push_external_audio_frame(
                data,
                timestamp_ms,
            )
        except Exception as exc:
            self.push_errors += 1
            self.last_error = str(exc)
            self._running = False
            return False
        self.last_result = int(result)
        if result == 0:
            self.pushed_frames += 1
        else:
            self.push_errors += 1
        self._next_frame += 1
        return True
