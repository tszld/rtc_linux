from __future__ import annotations

import base64
import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path

from tools.artc_poc_token import (
    build_artc_token,
    load_credentials,
    write_token_file,
)


class ArtcPocTokenTests(unittest.TestCase):
    def test_loads_credentials_from_unified_server_section(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.json"
            path.write_text(
                json.dumps(
                    {
                        "server": {
                            "app_id": "test-app",
                            "app_key": "test-key",
                        }
                    }
                ),
                encoding="utf-8",
            )

            self.assertEqual(
                load_credentials(path),
                ("test-app", "test-key"),
            )

    def test_matches_official_fixed_hash_vector(self) -> None:
        token = build_artc_token(
            "abc",
            "abckey",
            "abcChannel",
            "abcUser",
            expires_at=1699423634,
        )

        decoded = json.loads(base64.b64decode(token))

        self.assertEqual(decoded["appid"], "abc")
        self.assertEqual(decoded["channelid"], "abcChannel")
        self.assertEqual(decoded["userid"], "abcUser")
        self.assertEqual(decoded["nonce"], "")
        self.assertEqual(decoded["timestamp"], 1699423634)
        self.assertEqual(
            decoded["token"],
            hashlib.sha256(b"abcabckeyabcChannelabcUser1699423634").hexdigest(),
        )

    def test_rejects_invalid_channel_identifier(self) -> None:
        with self.assertRaisesRegex(ValueError, "ChannelID"):
            build_artc_token(
                "app",
                "secret",
                "频道/非法",
                "robot-1",
                expires_at=9999999999,
            )

    def test_token_does_not_contain_app_key(self) -> None:
        app_key = "do-not-leak-this-key"
        token = build_artc_token(
            "app",
            app_key,
            "channel-1",
            "robot-1",
            expires_at=9999999999,
        )

        self.assertNotIn(app_key.encode("utf-8"), base64.b64decode(token))

    @unittest.skipUnless(os.name == "posix", "需要 POSIX 文件权限语义")
    def test_token_file_uses_private_permissions(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "robot.token"

            write_token_file(path, "short-lived-token")

            self.assertEqual(path.read_text(encoding="utf-8"), "short-lived-token\n")
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
