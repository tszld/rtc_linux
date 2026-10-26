from __future__ import annotations

import os
import shutil
import subprocess
import time
from collections.abc import Sequence
from typing import BinaryIO, Protocol


class ExternalVideoEngine(Protocol):
    def push_external_video_frame(
        self,
        data: bytes,
        width: int,
        height: int,
        timestamp_ms: int,
    ) -> int:
        ...


class CameraPublisher:
    """Capture a V4L2 camera as RGB24 and push live frames to ARTC."""

    def __init__(
        self,
        engine: ExternalVideoEngine,
        *,
        device: str = "/dev/video0",
        width: int = 640,
        height: int = 480,
        fps: int = 15,
        command: Sequence[str] | None = None,
    ) -> None:
        if width <= 0 or height <= 0 or fps <= 0:
            raise ValueError("Camera dimensions and fps must be positive")
        self.engine = engine
        self.device = device
        self.width = width
        self.height = height
        self.fps = fps
        self.frame_bytes = width * height * 3
        self.command = list(
            command
            or [
                "ffmpeg",
                "-nostdin",
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "v4l2",
                "-input_format",
                "yuyv422",
                "-video_size",
                f"{width}x{height}",
                "-framerate",
                str(fps),
                "-i",
                device,
                "-pix_fmt",
                "rgb24",
                "-f",
                "rawvideo",
                "pipe:1",
            ]
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
                f"Camera capture command not found: {self.command[0]}"
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
                f"Failed to start camera capture: {exc}"
            ) from exc
        if process.stdout is None or process.stderr is None:
            process.terminate()
            raise RuntimeError("Camera capture pipes were not created")
        os.set_blocking(process.stdout.fileno(), False)
        os.set_blocking(process.stderr.fileno(), False)
        self._process = process
        self._running = True

    def prime(
        self,
        frame_count: int = 2,
        *,
        timeout_seconds: float = 3.0,
    ) -> None:
        if not self.is_running:
            raise RuntimeError("Camera capture is not running")
        target = self.pushed_frames + max(1, frame_count)
        deadline = time.monotonic() + max(0.1, timeout_seconds)
        while self.pushed_frames < target and time.monotonic() < deadline:
            self.pump()
            if self.pushed_frames < target:
                time.sleep(0.02)
        if self.pushed_frames < target:
            self.last_error = (
                f"No RGB video frames received from {self.device}"
            )
            raise RuntimeError(self.last_error)

    def pump(self) -> None:
        if self._process is None or not self._running:
            return
        self._read_available()
        self._push_latest_frame()
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

    @staticmethod
    def _read_stream(stream: BinaryIO, output: bytearray) -> None:
        while True:
            try:
                chunk = stream.read(65536)
            except BlockingIOError:
                return
            if not chunk:
                return
            output.extend(chunk)

    def _push_latest_frame(self) -> None:
        complete_frames = len(self._buffer) // self.frame_bytes
        if complete_frames <= 0:
            return
        if complete_frames > 1:
            drop_frames = complete_frames - 1
            drop_bytes = drop_frames * self.frame_bytes
            del self._buffer[:drop_bytes]
            self.dropped_frames += drop_frames

        frame = bytes(self._buffer[: self.frame_bytes])
        del self._buffer[: self.frame_bytes]
        try:
            result = self.engine.push_external_video_frame(
                frame,
                self.width,
                self.height,
                self._next_timestamp_ms,
            )
        except Exception as exc:
            self.push_errors += 1
            self.last_error = str(exc)
            raise RuntimeError(
                f"Failed to push camera video: {exc}"
            ) from exc
        self.last_result = int(result)
        self._next_timestamp_ms += max(1, round(1000 / self.fps))
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
            f"Camera capture exited with code {process.returncode}"
        )
        self._running = False
        raise RuntimeError(self.last_error)
