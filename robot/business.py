from __future__ import annotations

import argparse
import asyncio
import queue
import sys
import time
from collections.abc import Sequence
from typing import Any

from .api import RobotApiError, RobotBackendClient
from .config import RobotConfig
from .heartbeat import RobotHeartbeatWorker
from .media import RobotMediaController
from .ws import RobotWebSocketReader


class RobotBusinessRuntime:
    def __init__(
        self,
        config: RobotConfig,
        *,
        api: RobotBackendClient | None = None,
        media: RobotMediaController | None = None,
    ) -> None:
        self.config = config
        self.events: queue.Queue[dict[str, Any]] = queue.Queue()
        self.api = api or RobotBackendClient(
            backend_url=config.backend_url,
            robot_id=config.robot_id,
            credential=config.credential,
        )
        self.media = media or RobotMediaController(config)
        self.ws = RobotWebSocketReader(
            self.api,
            self.events,
            reconnect_seconds=config.reconnect_seconds,
        )
        self.heartbeat = RobotHeartbeatWorker(
            self.api,
            self.events,
            interval_seconds=config.heartbeat_seconds,
        )
        self.automatic_speech_paused = False
        self.takeover_active = False
        self.robot_microphone_requested = False
        self.current_session_id: str | None = None
        self.ws_connected = False
        self.running = True

    def run(self, *, run_seconds: float | None = None) -> int:
        asyncio.set_event_loop(asyncio.new_event_loop())
        print(
            f"机器人连接控制端：{self.config.backend_url}",
            flush=True,
        )
        deadline = (
            time.monotonic() + run_seconds
            if run_seconds is not None
            else None
        )
        authenticated = False
        try:
            login = self._wait_for_approval(deadline)
            authenticated = True
            print(
                f"机器人已登录：{login['robot']['displayName']} "
                f"channel={login['robot']['channelId']}",
                flush=True,
            )
            self.api.heartbeat()
            self.ws.start()
            self.heartbeat.start()
            while self.running:
                if deadline is not None and time.monotonic() >= deadline:
                    break
                self._process_events()
                self.media.pump()
                try:
                    event = self.events.get(timeout=0.05)
                except queue.Empty:
                    continue
                self._handle_event(event)
            return 0
        except KeyboardInterrupt:
            return 0
        finally:
            if authenticated:
                self.stop()
            else:
                self.running = False

    def _wait_for_approval(self, deadline: float | None) -> dict[str, Any]:
        last_message = ""
        registered = False
        while self.running:
            try:
                return self.api.login()
            except RobotApiError as exc:
                if exc.status_code == 401 and not registered:
                    try:
                        result = self.api.register(self.config.display_name)
                    except RobotApiError as register_error:
                        raise RuntimeError(
                            f"机器人自动注册失败：{register_error}"
                        ) from register_error
                    registered = True
                    state = result.get("registrationState", "pending")
                    message = (
                        f"机器人已自动注册，等待管理员审批：{result.get('displayName', '')}"
                        if state == "pending"
                        else "机器人注册状态已确认，正在登录。"
                    )
                elif exc.status_code == 403 and (
                    "审批" in str(exc) or "停用" in str(exc)
                ):
                    message = f"机器人等待管理员处理：{exc}"
                else:
                    raise
                if message != last_message:
                    print(message, flush=True)
                    last_message = message

            if deadline is not None and time.monotonic() >= deadline:
                raise RuntimeError("等待机器人审批超时")
            time.sleep(min(2.0, self.config.reconnect_seconds))
        raise RuntimeError("机器人运行已停止")

    def stop(self) -> None:
        self.running = False
        self.ws.stop()
        self.heartbeat.stop()
        if self.media.is_active:
            try:
                self.media.set_microphone(False)
                if self.current_session_id:
                    self.api.end_session(self.current_session_id)
            except Exception:
                pass
        metrics = self.media.metrics()
        self.media.stop()
        print(
            "机器人接收音频统计："
            f"frames={metrics['remoteAudioFrames']} "
            f"bytes={metrics['remoteAudioBytes']} "
            f"peak={metrics['remoteAudioPeak']}",
            flush=True,
        )
        self.api.disconnect()

    def _process_events(self) -> None:
        while True:
            try:
                event = self.events.get_nowait()
            except queue.Empty:
                return
            self._handle_event(event)

    def _handle_event(self, event: dict[str, Any]) -> None:
        event_type = str(event.get("type", ""))
        if event_type == "ws_connected":
            self.ws_connected = True
            return
        if event_type == "connection_lost":
            self.ws_connected = False
            self._stop_media()
            return
        if event_type == "snapshot":
            requested = bool(event.get("robotMicrophoneRequested", False))
            session = event.get("session")
            if session:
                self._start_or_follow_session(session)
                self.robot_microphone_requested = requested
                self._apply_microphone_policy()
                self._report_media()
            elif self.config.auto_start_session and self.ws_connected:
                self._start_session()
            return
        if event_type == "session_started":
            self._start_or_follow_session(event["session"])
            return
        if event_type == "session_ended":
            if event.get("session", {}).get("id") == self.current_session_id:
                self._stop_media()
                if self.config.exit_when_session_ends:
                    print("业务会话已结束，机器人进程退出。", flush=True)
                    self.running = False
            return
        if event_type == "agent_takeover":
            self.takeover_active = True
            self.automatic_speech_paused = True
            self._apply_microphone_policy()
            self._report_media()
            print(
                "人工接管开始；自动语音暂停标记已设置，"
                f"客服={event.get('lease', {}).get('agentDisplayName', '')}",
                flush=True,
            )
            return
        if event_type in {"takeover_released", "takeover_expired"}:
            self.takeover_active = False
            self.automatic_speech_paused = False
            self._apply_microphone_policy()
            self._report_media()
            print("人工接管结束；自动语音暂停标记已清除。", flush=True)
            return
        if event_type == "listen_changed":
            self.robot_microphone_requested = bool(
                event.get("robotMicrophoneRequested")
            )
            self._apply_microphone_policy()
            self._report_media()
            return
        if event_type in {"heartbeat_error", "protocol_error", "error"}:
            print(
                f"机器人控制面消息：{event_type}: {event.get('message', '')}",
                file=sys.stderr,
                flush=True,
            )

    def _start_or_follow_session(self, session: dict[str, Any]) -> None:
        session_id = str(session.get("id", ""))
        if not session_id or session.get("status") == "ended":
            return
        if self.media.is_active and self.current_session_id == session_id:
            return
        self._stop_media()
        self.current_session_id = session_id
        auth = self.api.rtc_token()
        self.media.start(auth, session_id)
        self._apply_microphone_policy()
        self._report_media()
        print(f"机器人业务会话已入会：{session_id}", flush=True)

    def _start_session(self) -> None:
        try:
            session = self.api.start_session()
        except RobotApiError as exc:
            if "已有未结束" not in str(exc):
                print(f"开始业务会话失败：{exc}", file=sys.stderr, flush=True)
            return
        self._start_or_follow_session(session)

    def _apply_microphone_policy(self) -> None:
        if not self.media.is_active:
            return
        # 机器人麦克风属于默认开启的业务上行；客服是否听取不再控制它。
        should_publish = True
        try:
            self.media.set_microphone(should_publish)
        except Exception as exc:
            print(f"切换机器人麦克风失败：{exc}", file=sys.stderr, flush=True)

    def _report_media(self) -> None:
        if not self.current_session_id:
            return
        try:
            self.api.media_state(
                session_id=self.current_session_id,
                microphone_published=self.media.microphone_enabled,
                camera_published=False,
                automatic_speech_paused=self.automatic_speech_paused,
            )
        except RobotApiError as exc:
            print(f"上报媒体状态失败：{exc}", file=sys.stderr, flush=True)

    def _stop_media(self) -> None:
        if self.media.is_active:
            try:
                self.media.set_microphone(False)
                if self.current_session_id:
                    self.api.media_state(
                        session_id=self.current_session_id,
                        microphone_published=False,
                        camera_published=False,
                        automatic_speech_paused=False,
                    )
            except Exception:
                pass
        self.media.stop()
        self.current_session_id = None
        self.takeover_active = False
        self.automatic_speech_paused = False
        self.robot_microphone_requested = False


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="ARTC Linux 业务机器人")
    parser.add_argument("--run-seconds", type=float)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    if not sys.platform.startswith("linux"):
        print("ARTC 机器人端只支持 Linux。", file=sys.stderr)
        return 1
    args = parse_args(argv)
    try:
        config = RobotConfig.load()
    except ValueError as exc:
        print(f"机器人配置错误：{exc}", file=sys.stderr)
        return 1
    runtime = RobotBusinessRuntime(config)
    return runtime.run(run_seconds=args.run_seconds)


if __name__ == "__main__":
    raise SystemExit(main())
