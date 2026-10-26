from __future__ import annotations

import sys
import tempfile
import time
import unittest
from pathlib import Path

from agent.native_audio import (
    COMBO_VOICE_DEFAULTS,
    COMBO_VOICE_PRESETS,
    VOICE_MODE_SDK_NAMES,
    VOICE_MODES,
    NativeAudioError,
    NativeAudioManager,
    normalize_combo_voice,
)


FAKE_HELPER = r"""
import sys

print("READY", flush=True)
for raw_line in sys.stdin:
    fields = raw_line.strip().split("\t")
    if not fields:
        continue
    if fields[0] == "JOIN":
        print("JOIN_REQUEST\t0", flush=True)
        print("JOINED\t0\tchannel-test\ttalk-user\t12", flush=True)
    elif fields[0] == "MIC":
        print(f"MIC_RESULT\t0\t{fields[1]}", flush=True)
    elif fields[0] == "VOICE":
        print(f"VOICE_RESULT\t0\t{fields[1]}", flush=True)
    elif fields[0] == "COMBO":
        print(
            "COMBO_RESULT\t0\t"
            + "\t".join(fields[1:])
            + "\tcombination_high_gain",
            flush=True,
        )
    elif fields[0] == "TOKEN":
        print("TOKEN_RESULT\t0", flush=True)
    elif fields[0] == "STOP":
        print("LEFT\t0\t1", flush=True)
        print("STOPPED", flush=True)
        break
"""


class NativeAudioManagerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.helper_path = Path(self.temporary.name) / "fake_helper.py"
        self.helper_path.write_text(FAKE_HELPER, encoding="utf-8")
        self.manager = NativeAudioManager(
            command=[sys.executable, str(self.helper_path)],
            heartbeat_timeout_seconds=5,
        )

    def tearDown(self) -> None:
        self.manager.stop()
        self.temporary.cleanup()

    def test_missing_helper_is_reported(self) -> None:
        manager = NativeAudioManager(
            helper_path=Path(self.temporary.name) / "missing.exe"
        )

        self.assertFalse(manager.available)
        with self.assertRaisesRegex(NativeAudioError, "不存在"):
            manager.start(
                {"token": "token"},
                session_id="session-1",
                voice_mode=0,
            )

    def test_voice_mode_mapping_contains_all_sdk_7_6_modes(self) -> None:
        self.assertEqual(sorted(VOICE_MODES), [*range(12), 100, 101, 102])
        self.assertEqual(
            sorted(VOICE_MODE_SDK_NAMES),
            [*range(12), 100, 101, 102],
        )
        self.assertEqual(
            VOICE_MODE_SDK_NAMES[11],
            "AliEngineAudioEffectVoiceChangerPhonograph",
        )
        self.assertEqual(VOICE_MODES[100], "自定义组合音色（可调女声）")
        self.assertEqual(COMBO_VOICE_PRESETS[101]["baseVoiceMode"], 0)
        self.assertEqual(COMBO_VOICE_PRESETS[102]["baseVoiceMode"], 3)

    def test_combo_voice_rejects_out_of_range_pitch(self) -> None:
        invalid = {**COMBO_VOICE_DEFAULTS, "pitch": 2.5}

        with self.assertRaisesRegex(ValueError, "音调"):
            normalize_combo_voice(invalid)

    def test_start_voice_microphone_token_and_stop(self) -> None:
        started = self.manager.start(
            {"token": "token-1"},
            session_id="session-1",
            voice_mode=4,
        )

        self.assertTrue(started["running"])
        self.assertTrue(started["joined"])
        self.assertEqual(started["userId"], "talk-user")
        self.assertEqual(started["voiceMode"], 4)

        microphone = self.manager.set_microphone(True)
        self.assertTrue(microphone["microphoneEnabled"])

        voice = self.manager.set_voice_mode(100, COMBO_VOICE_DEFAULTS)
        self.assertEqual(voice["voiceMode"], 100)
        self.assertEqual(voice["comboVoice"]["baseVoiceMode"], 3)

        preset = self.manager.set_voice_mode(
            101,
            {**COMBO_VOICE_DEFAULTS, "baseVoiceMode": 11},
        )
        self.assertEqual(preset["voiceMode"], 101)
        self.assertEqual(preset["comboVoice"]["baseVoiceMode"], 0)

        refreshed = self.manager.refresh_token("token-2")
        self.assertTrue(refreshed["joined"])
        self.assertTrue(self.manager.heartbeat()["running"])

        stopped = self.manager.stop()
        self.assertFalse(stopped["running"])
        self.assertFalse(stopped["microphoneEnabled"])

    def test_watchdog_stops_helper_without_heartbeat(self) -> None:
        manager = NativeAudioManager(
            command=[sys.executable, str(self.helper_path)],
            heartbeat_timeout_seconds=0.2,
        )
        try:
            manager.start(
                {"token": "token"},
                session_id="session-1",
                voice_mode=0,
            )
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                if not manager.status()["running"]:
                    break
                time.sleep(0.05)
            self.assertFalse(manager.status()["running"])
        finally:
            manager.stop()


if __name__ == "__main__":
    unittest.main()
