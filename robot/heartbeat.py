from __future__ import annotations

import queue
import threading
from typing import Any

from .api import RobotBackendClient


class RobotHeartbeatWorker:
    def __init__(
        self,
        api: RobotBackendClient,
        events: queue.Queue[dict[str, Any]],
        *,
        interval_seconds: float,
    ) -> None:
        self.api = api
        self.events = events
        self.interval_seconds = interval_seconds
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run,
            name="robot-heartbeat",
            daemon=True,
        )

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=3)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.api.heartbeat()
            except Exception as exc:
                self.events.put(
                    {
                        "type": "heartbeat_error",
                        "message": str(exc),
                    }
                )
            self._stop.wait(self.interval_seconds)
