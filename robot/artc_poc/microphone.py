from __future__ import annotations

import os
import shutil
import subprocess
import time
from collections.abc import Sequence
from typing import BinaryIO, Protocol


class ExternalAudioEngine(Protocol):
    def push_external_audio_frame(self, data: bytes, timestamp_ms: int) -> int:
        ...


class MicrophonePublisher:
    """Capture the default Linux input source and push 10 ms PCM frames."""

    def __init__(
        self,
        engine: ExternalAudioEngine,
        *,
        sample_rate: int = 16000,
        channels: int = 1,
        frame_ms: int = 10,
        command: Sequence[str] | None = None,
    ) -> None:
        if sample_rate <= 0 or channels <= 0 or frame_ms <= 0:
            raise ValueError("PCM settings must be positive")
        self.engine = engine
        self.sample_rate = sample_rate
        self.channels = channels
        self.frame_ms = frame_ms
        self.frame_bytes = (
            sample_rate * frame_ms // 1000 * channels * 2
        )
        if self.frame_bytes <= 0:
            raise ValueError("frame_ms is too short for a PCM frame")
        self.command = list(
            command
            or _default_capture_command(sample_rate, channels)
        )
        self.pushed_frames = 0
        self.push_errors = 0
        self.dropped_frames = 0
        self.last_result = 0
        self.last_error = ""
        self._process: subprocess.Popen[bytes] | None = None
        self._buffer = bytearray()
        self._stderr = bytearray()
        self._running = False
        self._next_timestamp_ms = 0

    @property
    def is_running(self) -> bool:
        process = self._process
        return bool(self._running and process and process.poll() is None)

    def start(self) -> None:
        if self.is_running:
            return
        if self._process is not None:
            self.stop()
        executable = shutil.which(self.command[0])
        if not executable:
            raise RuntimeError(
                f"Audio capture command not found: {self.command[0]}"
            )
        self._reset_state()
        command = [executable, *self.command[1:]]
        try:
            process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0,
            )
        except OSError as exc:
            raise RuntimeError(
                f"Failed to start audio capture: {exc}"
            ) from exc
        if process.stdout is None or process.stderr is None:
            process.terminate()
            raise RuntimeError("Audio capture pipes were not created")
        os.set_blocking(process.stdout.fileno(), False)
        os.set_blocking(process.stderr.fileno(), False)
        self._process = process
        self._running = True

    def prime(
        self,
        frame_count: int = 5,
        *,
        timeout_seconds: float = 3.0,
    ) -> None:
        if not self.is_running:
            raise RuntimeError("Audio capture is not running")
        target = self.pushed_frames + max(1, frame_count)
        deadline = time.monotonic() + max(0.1, timeout_seconds)
        while self.pushed_frames < target and time.monotonic() < deadline:
            self._read_available()
            self._push_frames()
            self._raise_if_process_failed()
            if self.pushed_frames < target:
                time.sleep(0.01)
        if self.pushed_frames < target:
            self.last_error = "No PCM data received from the default input source"
            raise RuntimeError(self.last_error)

    def pump(self) -> None:
        if self._process is None or not self._running:
            return
        self._read_available()
        self._push_frames()
        self._raise_if_process_failed()

    def stop(self) -> None:
        process = self._process
        self._process = None
        self._running = False
        if process is None:
            return
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=1)
        for stream in (process.stdout, process.stderr):
            if stream is not None:
                stream.close()

    def _reset_state(self) -> None:
        self.pushed_frames = 0
        self.push_errors = 0
        self.dropped_frames = 0
        self.last_result = 0
        self.last_error = ""
        self._buffer.clear()
        self._stderr.clear()
        self._next_timestamp_ms = 0

    def _read_available(self) -> None:
        process = self._process
        if process is None:
            return
        if process.stdout is not None:
            self._read_stream(process.stdout, self._buffer)
        if process.stderr is not None:
            self._read_stream(process.stderr, self._stderr)
        max_buffer_bytes = self.frame_bytes * 100
        if len(self._buffer) > max_buffer_bytes:
            excess = len(self._buffer) - max_buffer_bytes
            drop_bytes = (
                (excess + self.frame_bytes - 1) // self.frame_bytes
            ) * self.frame_bytes
            del self._buffer[:drop_bytes]
            self.dropped_frames += drop_bytes // self.frame_bytes

    @staticmethod
    def _read_stream(stream: BinaryIO, output: bytearray) -> None:
        while True:
            try:
                chunk = stream.read(8192)
            except BlockingIOError:
                return
            if not chunk:
                return
            output.extend(chunk)

    def _push_frames(self) -> None:
        while len(self._buffer) >= self.frame_bytes:
            frame = bytes(self._buffer[: self.frame_bytes])
            del self._buffer[: self.frame_bytes]
            try:
                result = self.engine.push_external_audio_frame(
                    frame,
                    self._next_timestamp_ms,
                )
            except Exception as exc:
                self.push_errors += 1
                self.last_error = str(exc)
                raise RuntimeError(
                    f"Failed to push microphone audio: {exc}"
                ) from exc
            self.last_result = int(result)
            self._next_timestamp_ms += self.frame_ms
            if result == 0:
                self.pushed_frames += 1
            else:
                self.push_errors += 1

    def _raise_if_process_failed(self) -> None:
        process = self._process
        if process is None or process.poll() is None:
            return
        self._read_available()
        detail = self._stderr.decode("utf-8", errors="replace").strip()
        self.last_error = detail or (
            f"Audio capture exited with code {process.returncode}"
        )
        self._running = False
        raise RuntimeError(self.last_error)


def _default_capture_command(
    sample_rate: int,
    channels: int,
) -> list[str]:
    if shutil.which("parecord"):
        return [
            "parecord",
            "--raw",
            "--format=s16le",
            f"--rate={sample_rate}",
            f"--channels={channels}",
            "--latency-msec=20",
        ]
    if shutil.which("pw-record"):
        return [
            "pw-record",
            "--format",
            "s16",
            "--rate",
            str(sample_rate),
            "--channels",
            str(channels),
            "-",
        ]
    return [
        "parecord",
        "--raw",
        "--format=s16le",
        f"--rate={sample_rate}",
        f"--channels={channels}",
        "--latency-msec=20",
    ]
