from __future__ import annotations

import json
import queue
import threading
import time
from typing import Any

from websockets.sync.client import connect

from .api import RobotApiError, RobotBackendClient


class RobotWebSocketReader:
    def __init__(
        self,
        api: RobotBackendClient,
        events: queue.Queue[dict[str, Any]],
        *,
        reconnect_seconds: float,
    ) -> None:
        self.api = api
        self.events = events
        self.reconnect_seconds = reconnect_seconds
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run,
            name="robot-ws",
            daemon=True,
        )

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=5)

    def _run(self) -> None:
        while not self._stop.is_set():
            connection = None
            try:
                ticket = self.api.ws_ticket()
                connection = connect(
                    f"{self.api.ws_url}?ticket={ticket}",
                    open_timeout=10,
                    proxy=None,
                )
                self.events.put({"type": "ws_connected"})
                last_ping = time.monotonic()
                while not self._stop.is_set():
                    try:
                        raw = connection.recv(timeout=1)
                    except TimeoutError:
                        if time.monotonic() - last_ping >= 10:
                            connection.send(json.dumps({"type": "ping"}))
                            last_ping = time.monotonic()
                        continue
                    if isinstance(raw, bytes):
                        raw = raw.decode("utf-8", errors="replace")
                    try:
                        message = json.loads(raw)
                    except json.JSONDecodeError:
                        self.events.put(
                            {
                                "type": "protocol_error",
                                "message": "机器人 WebSocket 收到无效 JSON",
                            }
                        )
                        continue
                    if isinstance(message, dict):
                        self.events.put(message)
            except Exception as exc:
                self.events.put(
                    {
                        "type": "connection_lost",
                        "message": str(exc),
                    }
                )
            finally:
                if connection is not None:
                    try:
                        connection.close()
                    except Exception:
                        pass
            self._stop.wait(self.reconnect_seconds)
