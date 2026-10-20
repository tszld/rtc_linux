from __future__ import annotations

import json
import re
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.request import Request, urlopen

from caller import launcher


class CallerLauncherTests(unittest.TestCase):
    def test_configured_name_has_priority_over_generated_name(self) -> None:
        config = {
            "default_name": "配置固定名称",
            "generated_display_name": "CALL-12345678",
        }

        self.assertEqual(
            launcher.ensure_client_display_name(config),
            "配置固定名称",
        )

    def test_empty_configured_name_generates_and_reuses_local_name(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            local_path = Path(temp) / "config.local.json"
            with patch.object(launcher, "LOCAL_CONFIG_PATH", local_path):
                launcher.save_local_config({"client_credential": "kept-credential"})
                config = {"default_name": "   "}
                generated = launcher.ensure_client_display_name(config)
                self.assertRegex(generated, r"^CALL-[0-9A-F]{8}$")

                stored = json.loads(local_path.read_text(encoding="utf-8"))
                self.assertEqual(
                    stored["generated_display_name"],
                    generated,
                )
                self.assertEqual(
                    stored["client_credential"],
                    "kept-credential",
                )

                reloaded = {"default_name": "", **stored}
                self.assertEqual(
                    launcher.ensure_client_display_name(reloaded),
                    generated,
                )

    def test_identity_is_persisted_and_credential_is_saved(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            local_path = Path(temp) / "config.local.json"
            config = {"default_name": "测试客户端"}
            with patch.object(launcher, "LOCAL_CONFIG_PATH", local_path):
                installation_uuid = launcher.ensure_installation_uuid(config)
                self.assertTrue(installation_uuid)
                stored = json.loads(local_path.read_text(encoding="utf-8"))
                self.assertEqual(
                    stored["installation_uuid"],
                    installation_uuid,
                )

                public_config = {
                    "installationUuid": installation_uuid,
                    "clientCredential": None,
                }
                server = ThreadingHTTPServer(
                    ("127.0.0.1", 0),
                    launcher.make_handler(public_config, threading.Lock()),
                )
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                try:
                    request = Request(
                        f"http://127.0.0.1:{server.server_port}/client-credential",
                        method="POST",
                        data=json.dumps(
                            {"client_credential": "credential-value"}
                        ).encode("utf-8"),
                        headers={"Content-Type": "application/json"},
                    )
                    with urlopen(request, timeout=3) as response:
                        self.assertEqual(response.status, 200)

                    reset_request = Request(
                        f"http://127.0.0.1:{server.server_port}/client-reset-identity",
                        method="POST",
                        data=b"{}",
                        headers={"Content-Type": "application/json"},
                    )
                    with urlopen(reset_request, timeout=3) as response:
                        self.assertEqual(response.status, 200)
                finally:
                    server.shutdown()
                    server.server_close()
                    thread.join(timeout=2)

                stored = json.loads(local_path.read_text(encoding="utf-8"))
                self.assertNotEqual(
                    stored["installation_uuid"],
                    installation_uuid,
                )
                self.assertIsNone(stored["client_credential"])
                self.assertEqual(
                    public_config["installationUuid"],
                    stored["installation_uuid"],
                )
                self.assertIsNone(public_config["clientCredential"])


if __name__ == "__main__":
    unittest.main()
