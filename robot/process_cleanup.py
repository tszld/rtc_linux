from __future__ import annotations

import os
import signal
import time
from pathlib import Path


MEDIA_PROCESS_NAMES = {
    "AliRtcCoreService",
    "ffmpeg",
    "ffplay",
    "parecord",
    "pw-record",
}


def cleanup_orphaned_media_processes(
    project_root: Path,
    *,
    proc_root: Path = Path("/proc"),
) -> list[int]:
    root = project_root.resolve()
    candidates: list[int] = []
    if not proc_root.is_dir():
        return candidates
    for entry in proc_root.iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        if pid == os.getpid() or _parent_pid(entry) != 1:
            continue
        try:
            executable = Path(os.readlink(entry / "exe")).name
            cwd = Path(os.readlink(entry / "cwd")).resolve()
        except OSError:
            continue
        if (
            executable in MEDIA_PROCESS_NAMES
            and (cwd == root or root in cwd.parents)
        ):
            candidates.append(pid)

    for pid in candidates:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
    deadline = time.monotonic() + 2
    remaining = list(candidates)
    while remaining and time.monotonic() < deadline:
        remaining = [pid for pid in remaining if _is_alive(pid)]
        if remaining:
            time.sleep(0.05)
    for pid in remaining:
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
    return candidates


def _parent_pid(entry: Path) -> int:
    try:
        status = (entry / "status").read_text(
            encoding="utf-8",
            errors="replace",
        )
    except OSError:
        return -1
    for line in status.splitlines():
        if line.startswith("PPid:"):
            try:
                return int(line.split(":", 1)[1].strip())
            except ValueError:
                return -1
    return -1


def _is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False
