from __future__ import annotations

import shutil
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
HARNESS = ROOT / "tests" / "rtc_subscription_harness.js"


class RtcSubscriptionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.node = shutil.which("node")
        if not cls.node:
            raise unittest.SkipTest("Node.js is required for RTC JavaScript tests")

    def run_case(self, case_name: str) -> None:
        result = subprocess.run(
            [self.node, str(HARNESS), case_name],
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
            check=False,
        )
        self.assertEqual(
            result.returncode,
            0,
            f"{case_name} failed\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}",
        )

    def test_caller_subscribes_to_mcu_audio(self) -> None:
        self.run_case("caller")

    def test_caller_publishes_and_stops_screen_track(self) -> None:
        self.run_case("caller-screen")

    def test_agent_subscribes_to_mcu_audio_and_remote_screen(self) -> None:
        self.run_case("agent")

    def test_agent_retries_screen_subscription_until_available(self) -> None:
        self.run_case("agent-screen-retry")

    def test_caller_retries_audio_subscription_until_available(self) -> None:
        self.run_case("caller-audio-retry")

    def test_agent_retries_audio_subscription_until_available(self) -> None:
        self.run_case("agent-audio-retry")


if __name__ == "__main__":
    unittest.main()
