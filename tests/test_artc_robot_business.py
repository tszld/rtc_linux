from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from robot.api import RobotApiError
from robot.business import RobotBusinessRuntime
from robot.config import RobotConfig


class FakeRobotApi:
    def __init__(self) -> None:
        self.media_states = []
        self.ended_sessions = []
        self.disconnected = False

    def login(self):
        return {
            "robot": {
                "displayName": "测试机器人",
                "channelId": "channel-1",
            }
        }

    def heartbeat(self):
        return {"status": "online"}

    def ws_ticket(self):
        return "ticket"

    def rtc_token(self):
        return {
            "token": "token",
            "channel": "channel-1",
            "userId": "robot-test",
            "userName": "测试机器人",
        }

    def media_state(self, **payload):
        self.media_states.append(payload)
        return payload

    def start_session(self):
        return {"id": "session-1", "status": "active"}

    def end_session(self, session_id: str):
        self.ended_sessions.append(session_id)
        return {"id": session_id, "status": "ended"}

    def disconnect(self):
        self.disconnected = True


class FakeRobotMedia:
    def __init__(self) -> None:
        self.started = False
        self.session_id = None
        self.microphone_enabled = False
        self.stopped = False

    @property
    def is_active(self) -> bool:
        return self.started

    def start(self, auth, session_id):
        self.started = True
        self.session_id = session_id

    def set_microphone(self, enabled):
        self.microphone_enabled = enabled

    def pump(self):
        return

    def state(self, *, automatic_speech_paused):
        return {
            "microphonePublished": self.microphone_enabled,
            "automaticSpeechPaused": automatic_speech_paused,
        }

    def stop(self):
        self.started = False
        self.session_id = None
        self.microphone_enabled = False
        self.stopped = True


class RobotBusinessRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.config = RobotConfig(
            backend_url="http://127.0.0.1:8000",
            robot_id="robot-1",
            credential="credential",
            sdk_root=Path(self.temp.name),
            log_dir=Path(self.temp.name) / "logs",
            auto_start_session=False,
            audio_output=False,
        )
        self.api = FakeRobotApi()
        self.media = FakeRobotMedia()
        self.runtime = RobotBusinessRuntime(
            self.config,
            api=self.api,
            media=self.media,
        )
        self.runtime.ws_connected = True

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_session_and_takeover_mic_policy(self) -> None:
        self.runtime._handle_event(
            {
                "type": "snapshot",
                "session": {"id": "session-1", "status": "active"},
            }
        )
        self.assertTrue(self.media.started)
        self.assertTrue(self.media.microphone_enabled)

        self.runtime._handle_event(
            {
                "type": "listen_changed",
                "robotMicrophoneRequested": True,
            }
        )
        self.assertTrue(self.media.microphone_enabled)

        self.runtime._handle_event(
            {
                "type": "agent_takeover",
                "lease": {"agentDisplayName": "客服 A"},
            }
        )
        self.assertTrue(self.media.microphone_enabled)
        self.assertTrue(self.api.media_states[-1]["automatic_speech_paused"])

        self.runtime._handle_event(
            {
                "type": "takeover_released",
                "lease": {"agentId": "agent-1"},
            }
        )
        self.assertTrue(self.media.microphone_enabled)
        self.assertFalse(self.api.media_states[-1]["automatic_speech_paused"])

        self.runtime._handle_event(
            {
                "type": "session_ended",
                "session": {"id": "session-1", "status": "ended"},
            }
        )
        self.assertFalse(self.media.started)
        self.assertFalse(self.runtime.running)

    def test_connection_loss_stops_media(self) -> None:
        self.runtime._handle_event(
            {
                "type": "snapshot",
                "session": {"id": "session-1", "status": "active"},
            }
        )
        self.runtime._handle_event(
            {"type": "connection_lost", "message": "network"}
        )

        self.assertFalse(self.media.started)
        self.assertFalse(self.runtime.ws_connected)

    def test_registration_pending_then_login_succeeds(self) -> None:
        class ApprovalApi(FakeRobotApi):
            def __init__(self) -> None:
                super().__init__()
                self.login_count = 0
                self.register_count = 0

            def login(self):
                self.login_count += 1
                if self.login_count == 1:
                    raise RobotApiError("机器人凭证无效", 401)
                if self.login_count == 2:
                    raise RobotApiError("机器人尚未审批", 403)
                return super().login()

            def register(self, display_name):
                self.register_count += 1
                return {
                    "displayName": display_name,
                    "registrationState": "pending",
                }

        api = ApprovalApi()
        runtime = RobotBusinessRuntime(
            self.config,
            api=api,
            media=self.media,
        )

        with patch("robot.business.time.sleep", return_value=None):
            login = runtime._wait_for_approval(deadline=None)

        self.assertEqual(login["robot"]["displayName"], "测试机器人")
        self.assertEqual(api.register_count, 1)
        self.assertEqual(api.login_count, 3)

    def test_snapshot_restores_persisted_listener_state(self) -> None:
        self.runtime._handle_event(
            {
                "type": "snapshot",
                "robotMicrophoneRequested": True,
                "session": {"id": "session-1", "status": "active"},
            }
        )

        self.assertTrue(self.media.started)
        self.assertTrue(self.media.microphone_enabled)


class RobotConfigTests(unittest.TestCase):
    def test_unified_config_and_camera_guard(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "config.json"
            payload = {
                "cluster": {
                    "control_url": "http://127.0.0.1:8000",
                },
                "robot": {
                    "robot_id": "robot-1",
                    "credential": "private-credential",
                    "sdk_root": "robot/vendor/sdk",
                    "auto_start_session": True,
                    "camera_enabled": False,
                },
            }
            path.write_text(json.dumps(payload), encoding="utf-8")

            config = RobotConfig.load(path)
            self.assertEqual(config.robot_id, "robot-1")
            self.assertEqual(config.credential, "private-credential")
            self.assertTrue(config.audio_output)
            self.assertTrue(config.exit_when_session_ends)
            self.assertIsNone(config.test_tone_frequency)

            payload["robot"]["camera_enabled"] = True
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "摄像头"):
                RobotConfig.load(path)


if __name__ == "__main__":
    unittest.main()
