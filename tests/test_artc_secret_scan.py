from __future__ import annotations

import json
import secrets
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


class SecretScanTests(unittest.TestCase):
    def test_secret_scan_does_not_report_allowed_configs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = root / "config.json"
            app_key = secrets.token_urlsafe(24)
            robot_credential = secrets.token_urlsafe(24)
            config.write_text(
                json.dumps(
                    {
                        "server": {
                            "app_id": "test-app",
                            "app_key": app_key,
                            "db_path": str(root / "db.sqlite"),
                        },
                        "robot": {
                            "robot_id": "robot-1",
                            "credential": robot_credential,
                        },
                    }
                ),
                encoding="utf-8",
            )
            result = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "tools" / "secret_scan.py"),
                    "--config",
                    str(config),
                ],
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            payload = json.loads(result.stdout)
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["leakPaths"], [])
            self.assertEqual(payload["credentialLeakPaths"], [])


if __name__ == "__main__":
    unittest.main()
