from __future__ import annotations

import json
import threading
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen


class RobotApiError(RuntimeError):
    def __init__(self, message: str, status_code: int = 0) -> None:
        super().__init__(message)
        self.status_code = status_code


class RobotBackendClient:
    def __init__(
        self,
        *,
        backend_url: str,
        robot_id: str,
        credential: str,
        timeout_seconds: float = 10,
    ) -> None:
        self.backend_url = backend_url.rstrip("/")
        self.robot_id = robot_id
        self._credential = credential
        self.timeout_seconds = timeout_seconds
        self._access_token = ""
        self._lock = threading.Lock()

    @property
    def ws_url(self) -> str:
        if self.backend_url.startswith("https://"):
            return "wss://" + self.backend_url.removeprefix("https://") + "/ws"
        if self.backend_url.startswith("http://"):
            return "ws://" + self.backend_url.removeprefix("http://") + "/ws"
        raise RobotApiError("backend_url 必须使用 http:// 或 https://")

    def login(self) -> dict[str, Any]:
        payload = self._request(
            "POST",
            "/api/robot/login",
            body={
                "robotId": self.robot_id,
                "credential": self._credential,
            },
            authenticated=False,
        )
        token = str(payload.get("accessToken", ""))
        if not token:
            raise RobotApiError("后端登录响应缺少 accessToken")
        with self._lock:
            self._access_token = token
        return payload

    def register(self, display_name: str) -> dict[str, Any]:
        return self._request(
            "POST",
            "/api/robot/register",
            body={
                "robotId": self.robot_id,
                "credential": self._credential,
                "displayName": display_name,
            },
            authenticated=False,
        )

    def _ensure_token(self) -> str:
        with self._lock:
            if not self._access_token:
                token = ""
            else:
                token = self._access_token
        if not token:
            self.login()
            with self._lock:
                token = self._access_token
        return token

    def request(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
    ) -> Any:
        return self._request(method, path, body=body, authenticated=True)

    def _request(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, Any] | None = None,
        authenticated: bool,
        retry_auth: bool = True,
    ) -> Any:
        headers = {"Accept": "application/json"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        if authenticated:
            headers["Authorization"] = f"Bearer {self._ensure_token()}"
        request = Request(
            self.backend_url + path,
            data=(
                json.dumps(body, ensure_ascii=False).encode("utf-8")
                if body is not None
                else None
            ),
            headers=headers,
            method=method,
        )
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                raw = response.read()
                return json.loads(raw) if raw else None
        except HTTPError as exc:
            try:
                error_payload = json.loads(exc.read())
            except (ValueError, OSError):
                error_payload = {}
            if (
                authenticated
                and retry_auth
                and exc.code == 401
                and path != "/api/robot/login"
            ):
                self.login()
                return self._request(
                    method,
                    path,
                    body=body,
                    authenticated=True,
                    retry_auth=False,
                )
            detail = error_payload.get("detail") or f"HTTP {exc.code}"
            raise RobotApiError(str(detail), exc.code) from exc
        except (URLError, TimeoutError) as exc:
            raise RobotApiError(f"无法连接业务后端：{exc}") from exc
        except json.JSONDecodeError as exc:
            raise RobotApiError("业务后端返回了无效 JSON") from exc

    def heartbeat(self) -> dict[str, Any]:
        return self.request("POST", "/api/robot/heartbeat")

    def rtc_token(self) -> dict[str, Any]:
        return self.request("POST", "/api/robot/rtc-token")

    def media_state(
        self,
        *,
        session_id: str | None,
        microphone_published: bool,
        camera_published: bool,
        automatic_speech_paused: bool,
    ) -> dict[str, Any]:
        return self.request(
            "POST",
            "/api/robot/media-state",
            {
                "sessionId": session_id,
                "microphonePublished": microphone_published,
                "cameraPublished": camera_published,
                "automaticSpeechPaused": automatic_speech_paused,
            },
        )

    def start_session(self) -> dict[str, Any]:
        return self.request("POST", "/api/robot/sessions/start")

    def end_session(self, session_id: str) -> dict[str, Any]:
        return self.request(
            "POST",
            f"/api/robot/sessions/{quote(session_id, safe='')}/end",
        )

    def ws_ticket(self) -> str:
        payload = self.request("GET", "/api/robot/ws-ticket")
        return str(payload["ticket"])

    def disconnect(self) -> None:
        try:
            self.request(
                "POST",
                "/api/robot/disconnect",
                {"reason": "robot_shutdown"},
            )
        except RobotApiError:
            pass
