from __future__ import annotations

import io
import tempfile
import unittest
from pathlib import Path

from robot.artc_poc.audio import NullPcmOutput, PcmFrame, apply_pcm_gain
from robot.artc_poc.main import (
    read_token,
    set_microphone_enabled,
    validate_token_scope,
)
from robot.artc_poc.microphone import MicrophonePublisher
from robot.artc_poc.session import PoCSessionError, RobotPoCSession
from robot.artc_poc.tone import TonePublisher, build_tone_frame
from tools.artc_poc_token import build_artc_token


class FakeEngine:
    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.join_result = 0

    def join(
        self,
        token: str,
        channel: str,
        user_id: str,
        user_name: str,
    ) -> int:
        self.calls.append(("join", token, channel, user_id, user_name))
        return self.join_result

    def set_microphone_enabled(self, enabled: bool) -> int:
        self.calls.append(("microphone", enabled))
        return 0

    def leave(self) -> int:
        self.calls.append(("leave",))
        return 0

    def release(self) -> int:
        self.calls.append(("release",))
        return 0


class RobotPoCSessionTests(unittest.TestCase):
    def make_session(self) -> tuple[RobotPoCSession, FakeEngine, NullPcmOutput]:
        output = NullPcmOutput()
        session = RobotPoCSession(output)
        engine = FakeEngine()
        session.attach_engine(engine)
        return session, engine, output

    def test_join_disables_microphone_before_join(self) -> None:
        session, engine, _ = self.make_session()

        session.join("token", "channel-1", "robot-1", "Robot")

        self.assertEqual(engine.calls[0], ("microphone", False))
        self.assertEqual(
            engine.calls[1],
            ("join", "token", "channel-1", "robot-1", "Robot"),
        )
        self.assertEqual(session.state, "joining")

        session.on_join_result(0, "channel-1", "robot-1")

        self.assertEqual(session.state, "joined")
        self.assertFalse(session.microphone_enabled)

    def test_microphone_requires_joined_state(self) -> None:
        session, _, _ = self.make_session()

        with self.assertRaisesRegex(PoCSessionError, "不能切换麦克风"):
            session.set_microphone_enabled(True)

    def test_microphone_toggles_only_after_join(self) -> None:
        session, engine, _ = self.make_session()
        session.join("token", "channel-1", "robot-1", "Robot")
        session.on_join_result(0, "channel-1", "robot-1")

        session.set_microphone_enabled(True)
        session.set_microphone_enabled(False)

        self.assertEqual(
            engine.calls[-2:],
            [("microphone", True), ("microphone", False)],
        )
        self.assertFalse(session.microphone_enabled)

    def test_audio_source_is_stopped_when_start_fails(self) -> None:
        class FailingPublisher:
            def __init__(self) -> None:
                self.stop_calls = 0

            def start(self) -> None:
                return

            def prime(self, frame_count: int) -> None:
                raise RuntimeError("capture failed")

            def stop(self) -> None:
                self.stop_calls += 1

        session, _, _ = self.make_session()
        session.join("token", "channel-1", "robot-1", "Robot")
        session.on_join_result(0, "channel-1", "robot-1")
        publisher = FailingPublisher()

        with self.assertRaisesRegex(RuntimeError, "capture failed"):
            set_microphone_enabled(session, publisher, True)

        self.assertEqual(publisher.stop_calls, 1)
        self.assertFalse(session.microphone_enabled)

    def test_remote_pcm_is_forwarded_to_output(self) -> None:
        session, _, output = self.make_session()
        frame = PcmFrame(b"\x00\x01" * 480, 48000, 1, 16)

        session.on_remote_audio(frame)

        self.assertEqual(output.frame_count, 1)
        self.assertEqual(output.byte_count, 960)
        self.assertEqual(session.remote_audio_frames, 1)

    def test_remote_audio_peak_is_tracked(self) -> None:
        session, _, _ = self.make_session()

        session.on_remote_audio(PcmFrame(b"\x18\xfc\xd0\x07", 48000, 1, 16))

        self.assertEqual(session.remote_audio_peak, 2000)

    def test_remote_playback_volume_applies_pcm_gain(self) -> None:
        pcm = b"\xe8\x03\x18\xfc"

        self.assertEqual(apply_pcm_gain(pcm, 100), pcm)
        self.assertEqual(
            apply_pcm_gain(pcm, 50),
            b"\xf4\x01\x0c\xfe",
        )
        self.assertEqual(apply_pcm_gain(pcm, 0), b"\x00\x00\x00\x00")

        session, _, output = self.make_session()
        session.set_remote_playback_volume(35)
        self.assertEqual(output.volume, 35)

    def test_close_stops_microphone_leaves_and_releases(self) -> None:
        session, engine, _ = self.make_session()
        session.join("token", "channel-1", "robot-1", "Robot")
        session.on_join_result(0, "channel-1", "robot-1")
        session.set_microphone_enabled(True)

        def pump(_seconds: float) -> None:
            session.on_leave_result(0)

        session.close(pump)

        self.assertEqual(
            engine.calls[-3:],
            [("microphone", False), ("leave",), ("release",)],
        )
        self.assertEqual(session.state, "stopped")
        self.assertFalse(session.microphone_enabled)

    def test_join_callback_failure_surfaces_sdk_error(self) -> None:
        session, engine, _ = self.make_session()
        engine.join_result = -1

        with self.assertRaisesRegex(PoCSessionError, "JoinChannel"):
            session.join("token", "channel-1", "robot-1", "Robot")

        self.assertEqual(session.state, "error")

    def test_unexpected_leave_during_join_finishes_wait(self) -> None:
        session, _, _ = self.make_session()
        session.join("token", "channel-1", "robot-1", "Robot")

        session.on_leave_result(16974340)

        self.assertTrue(session.join_finished.is_set())
        self.assertEqual(session.state, "error")
        self.assertIn("16974340", session.error_message)

    def test_publish_invalid_is_recorded_without_aborting_session(self) -> None:
        session, _, _ = self.make_session()
        session.join("token", "channel-1", "robot-1", "Robot")
        session.on_join_result(0, "channel-1", "robot-1")

        session.on_error("ERR_PUBLISH_INVALID", "官方 SDK 错误码 16974597")

        self.assertEqual(session.state, "joined")
        self.assertIn("ERR_PUBLISH_INVALID", session.last_error)

    def test_fatal_engine_error_aborts_session(self) -> None:
        session, _, _ = self.make_session()
        session.join("token", "channel-1", "robot-1", "Robot")

        session.on_error("ERR_NETWORK_DISCONNECT", "网络断开")

        self.assertEqual(session.state, "error")
        self.assertTrue(session.join_finished.is_set())


class RobotPocTokenFileTests(unittest.TestCase):
    def test_scope_validation_accepts_matching_token(self) -> None:
        token = build_artc_token(
            "app",
            "secret",
            "channel-1",
            "robot-1",
            expires_at=9999999999,
        )

        validate_token_scope(token, "channel-1", "robot-1")

    def test_scope_validation_rejects_mismatched_user(self) -> None:
        token = build_artc_token(
            "app",
            "secret",
            "channel-1",
            "robot-1",
            expires_at=9999999999,
        )

        with self.assertRaisesRegex(PoCSessionError, "UserId"):
            validate_token_scope(token, "channel-1", "robot-2")

    def test_token_file_must_be_private(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "robot.token"
            path.write_text("token", encoding="utf-8")
            path.chmod(0o644)

            with self.assertRaisesRegex(PoCSessionError, "权限过宽"):
                read_token(path)


class SyntheticToneTests(unittest.TestCase):
    def test_tone_frame_has_expected_size_and_samples(self) -> None:
        frame = build_tone_frame(
            16000,
            440,
            frame_ms=10,
            amplitude=0.2,
        )

        self.assertEqual(len(frame), 320)
        self.assertNotEqual(frame, b"\x00" * len(frame))

    def test_invalid_tone_settings_are_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "amplitude"):
            build_tone_frame(16000, 440, amplitude=1.5)

    def test_tone_publisher_primes_frames_without_a_background_loop(self) -> None:
        class FakeToneEngine:
            def __init__(self) -> None:
                self.frames: list[tuple[int, int]] = []

            def push_external_audio_frame(
                self,
                data: bytes,
                timestamp_ms: int,
            ) -> int:
                self.frames.append((len(data), timestamp_ms))
                return 0

        engine = FakeToneEngine()
        publisher = TonePublisher(engine, sample_rate=16000, frequency_hz=440)

        publisher.start()
        publisher.prime(5)
        publisher.stop()

        self.assertEqual(
            engine.frames,
            [(320, 0), (320, 10), (320, 20), (320, 30), (320, 40)],
        )
        self.assertEqual(publisher.pushed_frames, 5)
        self.assertEqual(publisher.push_errors, 0)


class MicrophonePublisherTests(unittest.TestCase):
    def test_pcm_is_split_into_ten_millisecond_frames(self) -> None:
        class FakeMicrophoneEngine:
            def __init__(self) -> None:
                self.frames: list[tuple[int, int]] = []

            def push_external_audio_frame(
                self,
                data: bytes,
                timestamp_ms: int,
            ) -> int:
                self.frames.append((len(data), timestamp_ms))
                return 0

        engine = FakeMicrophoneEngine()
        publisher = MicrophonePublisher(engine, sample_rate=16000)

        publisher._buffer.extend(b"\x01\x00" * 480)
        publisher._push_frames()

        self.assertEqual(
            engine.frames,
            [(320, 0), (320, 10), (320, 20)],
        )
        self.assertEqual(publisher.pushed_frames, 3)
        self.assertEqual(publisher.push_errors, 0)

    def test_capture_process_failure_is_not_silently_ignored(self) -> None:
        class FakeProcess:
            returncode = 1
            stdout = io.BytesIO()
            stderr = io.BytesIO(b"device disconnected")

            def poll(self) -> int:
                return self.returncode

        engine = type(
            "FakeMicrophoneEngine",
            (),
            {
                "push_external_audio_frame": lambda self, data, timestamp: 0,
            },
        )()
        publisher = MicrophonePublisher(engine, sample_rate=16000)
        publisher._process = FakeProcess()
        publisher._running = True

        with self.assertRaisesRegex(RuntimeError, "device disconnected"):
            publisher.pump()

        self.assertFalse(publisher._running)


if __name__ == "__main__":
    unittest.main()
