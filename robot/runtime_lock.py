from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path


class RobotAlreadyRunningError(RuntimeError):
    pass


class RobotProcessLock:
    def __init__(
        self,
        robot_id: str,
        *,
        lock_dir: Path | None = None,
    ) -> None:
        digest = hashlib.sha256(robot_id.encode("utf-8")).hexdigest()[:16]
        directory = lock_dir or Path(tempfile.gettempdir())
        self.path = directory / f"artc-robot-{digest}.lock"
        self._fd: int | None = None

    def __enter__(self) -> RobotProcessLock:
        self.acquire()
        return self

    def __exit__(self, *_args: object) -> None:
        self.release()

    def acquire(self) -> None:
        if self._fd is not None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            self._lock(fd)
        except OSError as exc:
            existing_pid = self._read_pid(fd)
            os.close(fd)
            detail = f"pid={existing_pid}" if existing_pid else "pid=unknown"
            raise RobotAlreadyRunningError(
                f"该机器人已有进程运行（{detail}），"
                f"锁文件：{self.path}"
            ) from exc
        os.lseek(fd, 0, os.SEEK_SET)
        os.ftruncate(fd, 0)
        os.write(fd, f"{os.getpid()}\n".encode("ascii"))
        os.fsync(fd)
        self._fd = fd

    def release(self) -> None:
        fd = self._fd
        self._fd = None
        if fd is None:
            return
        try:
            os.ftruncate(fd, 0)
            self._unlock(fd)
        finally:
            os.close(fd)

    @staticmethod
    def _lock(fd: int) -> None:
        if os.name == "nt":
            import msvcrt

            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            return
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

    @staticmethod
    def _unlock(fd: int) -> None:
        if os.name == "nt":
            import msvcrt

            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            return
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_UN)

    @staticmethod
    def _read_pid(fd: int) -> int | None:
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            raw = os.read(fd, 64).decode("ascii", errors="ignore").strip()
            return int(raw) if raw else None
        except (OSError, ValueError):
            return None
