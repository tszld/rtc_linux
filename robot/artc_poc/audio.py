from __future__ import annotations

import array
import queue
import shutil
import subprocess
import sys
import threading
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class PcmFrame:
    data: bytes
    sample_rate: int
    channels: int
    sample_bits: int


class PcmOutput(Protocol):
    def feed(self, frame: PcmFrame) -> None:
        ...

    def close(self) -> None:
        ...


def s16le_peak(data: bytes) -> int:
    """返回 16-bit little-endian PCM 样本的最大绝对值。"""
    if len(data) < 2:
        return 0
    samples = array.array("h")
    samples.frombytes(data[: len(data) - len(data) % 2])
    if sys.byteorder != "little":
        samples.byteswap()
    return max((abs(sample) for sample in samples), default=0)


class NullPcmOutput:
    """用于无声卡环境的显式静音输出。"""

    def __init__(self) -> None:
        self.frame_count = 0
        self.byte_count = 0

    def feed(self, frame: PcmFrame) -> None:
        self.frame_count += 1
        self.byte_count += len(frame.data)

    def close(self) -> None:
        return


class FfplayPcmOutput:
    """通过 ffplay 将 16-bit PCM 非阻塞播放到默认扬声器。"""

    def __init__(self, ffplay_path: str | None = None) -> None:
        resolved = ffplay_path or shutil.which("ffplay")
        if not resolved:
            raise RuntimeError(
                "未找到 ffplay；请安装 ffmpeg，或使用 --no-audio-output 仅验证控制状态"
            )
        self._ffplay_path = resolved
        self._frames: queue.Queue[PcmFrame | None] = queue.Queue(maxsize=100)
        self._format: tuple[int, int] | None = None
        self._process: subprocess.Popen[bytes] | None = None
        self._failure: str | None = None
        self._closed = False
        self._worker = threading.Thread(
            target=self._run,
            name="artc-pcm-player",
            daemon=True,
        )
        self._worker.start()

    @property
    def failure(self) -> str | None:
        return self._failure

    def feed(self, frame: PcmFrame) -> None:
        if self._closed:
            raise RuntimeError("PCM 输出已经关闭")
        if self._failure:
            raise RuntimeError(self._failure)
        if frame.sample_bits != 16:
            raise RuntimeError(
                f"ffplay 输出仅支持 16-bit PCM，收到 {frame.sample_bits}-bit"
            )
        frame_format = (frame.sample_rate, frame.channels)
        if self._format is None:
            self._format = frame_format
        elif self._format != frame_format:
            raise RuntimeError(
                f"远端 PCM 格式发生变化：{self._format} -> {frame_format}"
            )
        try:
            self._frames.put_nowait(frame)
        except queue.Full:
            # 回调线程不能阻塞；播放严重落后时丢弃最旧帧并保留最新音频。
            try:
                self._frames.get_nowait()
            except queue.Empty:
                pass
            self._frames.put_nowait(frame)

    def _run(self) -> None:
        try:
            while True:
                frame = self._frames.get()
                if frame is None:
                    break
                if self._process is None:
                    assert self._format is not None
                    sample_rate, channels = self._format
                    self._process = subprocess.Popen(
                        [
                            self._ffplay_path,
                            "-nodisp",
                            "-autoexit",
                            "-loglevel",
                            "error",
                            "-f",
                            "s16le",
                            "-ar",
                            str(sample_rate),
                            "-ac",
                            str(channels),
                            "-i",
                            "pipe:0",
                        ],
                        stdin=subprocess.PIPE,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.PIPE,
                    )
                if self._process.stdin is None:
                    raise RuntimeError("ffplay 标准输入不可用")
                self._process.stdin.write(frame.data)
                self._process.stdin.flush()
        except (BrokenPipeError, OSError, RuntimeError) as exc:
            self._failure = f"播放远端 PCM 失败：{exc}"
        finally:
            process = self._process
            if process is not None:
                if process.stdin:
                    try:
                        process.stdin.close()
                    except OSError:
                        pass
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.terminate()
                    try:
                        process.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        process.kill()
                if process.stderr:
                    process.stderr.close()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._frames.put_nowait(None)
        except queue.Full:
            try:
                self._frames.get_nowait()
            except queue.Empty:
                pass
            self._frames.put_nowait(None)
        self._worker.join(timeout=5)
        if self._worker.is_alive() and self._process is not None:
            self._process.terminate()
            self._worker.join(timeout=2)
        if self._worker.is_alive() and self._process is not None:
            self._process.kill()
            self._worker.join(timeout=2)


def create_pcm_output(audio_enabled: bool) -> PcmOutput:
    if audio_enabled:
        return FfplayPcmOutput()
    return NullPcmOutput()
