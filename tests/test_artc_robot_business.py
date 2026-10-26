from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from robot.api import RobotApiError
from robot.business import RobotBusinessRuntime
from robot.config import RobotConfig
from robot.process_cleanup import cleanup_orphaned_media_processes
from robot.runtime_lock import (
    RobotAlreadyRunningError,
    RobotProcessLock,
)


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
        self.camera_published = False
        self.agent_playback_volume = 100
        self.stopped = False

    @property
    def is_active(self) -> bool:
        return self.started

    @property
    def camera_enabled(self) -> bool:
        return self.camera_published

    def start(self, auth, session_id):
        self.started = True
        self.session_id = session_id

    def set_microphone(self, enabled):
        self.microphone_enabled = enabled

    def set_agent_playback_volume(self, volume):
        self.agent_playback_volume = volume

    def pump(self):
        return

    def state(self, *, automatic_speech_paused):
        return {
            "microphonePublished": self.microphone_enabled,
            "cameraPublished": self.camera_published,
            "automaticSpeechPaused": automatic_speech_paused,
        }

    def stop(self):
        self.started = False
        self.session_id = None
        self.microphone_enabled = False
        self.camera_published = False
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
        self.media.camera_published = True
        self.runtime._handle_event(
            {
                "type": "listen_changed",
                "robotMicrophoneRequested": True,
            }
        )
        self.assertTrue(self.api.media_states[-1]["camera_published"])

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
                "type": "agent_playback_volume_changed",
                "sessionId": "session-1",
                "volume": 42,
            }
        )
        self.assertEqual(self.media.agent_playback_volume, 42)

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
        self.assertTrue(self.runtime.running)
        self.assertIsNotNone(self.runtime.idle_deadline)

    def test_idle_timeout_exits_after_five_minutes_without_session(self) -> None:
        self.runtime.idle_deadline = time.monotonic() - 1

        self.runtime._exit_if_idle()

        self.assertFalse(self.runtime.running)

    def test_active_session_clears_idle_deadline(self) -> None:
        self.runtime._mark_no_session()
        self.assertIsNotNone(self.runtime.idle_deadline)

        self.runtime._mark_session_active()

        self.assertIsNone(self.runtime.idle_deadline)

    def test_robot_shutdown_event_stops_runtime(self) -> None:
        self.runtime._handle_event(
            {
                "type": "robot_shutdown",
                "reason": "agent_requested",
            }
        )

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
    def test_unified_config_and_camera_settings(self) -> None:
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
            self.assertEqual(config.idle_exit_seconds, 300)
            self.assertFalse(config.camera_enabled)
            self.assertEqual(config.camera_device, "/dev/video0")

            payload["robot"]["camera_enabled"] = True
            payload["robot"]["camera_device"] = "/dev/video1"
            path.write_text(json.dumps(payload), encoding="utf-8")
            config = RobotConfig.load(path)
            self.assertTrue(config.camera_enabled)
            self.assertEqual(config.camera_device, "/dev/video1")

            payload["robot"]["camera_device"] = ""
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "camera_device"):
                RobotConfig.load(path)


class RobotProcessLockTests(unittest.TestCase):
    def test_second_instance_is_rejected_until_lock_is_released(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            lock_dir = Path(temporary)
            first = RobotProcessLock("robot-1", lock_dir=lock_dir)
            second = RobotProcessLock("robot-1", lock_dir=lock_dir)

            first.acquire()
            try:
                with self.assertRaisesRegex(
                    RobotAlreadyRunningError,
                    "已有进程运行",
                ):
                    second.acquire()
            finally:
                first.release()

            self.assertEqual(first.path.read_text(encoding="ascii"), "")
            second.acquire()
            second.release()

    def test_orphaned_media_process_cleanup_targets_project_children(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = root / "project"
            proc = root / "proc"
            entry = proc / "123"
            entry.mkdir(parents=True)
            (entry / "status").write_text(
                "Name:\tffmpeg\nPPid:\t1\n",
                encoding="utf-8",
            )

            def readlink(path: Path) -> str:
                if path.name == "exe":
                    return str(project / "usr" / "bin" / "ffmpeg")
                if path.name == "cwd":
                    return str(project)
                raise OSError(path)

            with (
                patch(
                    "robot.process_cleanup.os.readlink",
                    side_effect=readlink,
                ),
                patch("robot.process_cleanup._is_alive", return_value=False),
                patch("robot.process_cleanup.os.kill") as kill,
            ):
                cleaned = cleanup_orphaned_media_processes(
                    project,
                    proc_root=proc,
                )

        self.assertEqual(cleaned, [123])
        kill.assert_called_once()


if __name__ == "__main__":
    unittest.main()
