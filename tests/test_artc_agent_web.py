from __future__ import annotations

import asyncio
import hashlib
import json
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from agent.config import AgentWebConfig
from agent.web_host import AgentWebServer
from project_config import load_project_config, resolve_bind_host
from server.app import create_app
from server.config import ServerConfig
from server.db import Database
from tests.asgi_client import request_bytes


ROOT = Path(__file__).resolve().parent.parent
AGENT_WEB_DIR = ROOT / "agent" / "web"
SDK_SHA256 = "46b57790f4539150f45c6ab3f113e9ed0b73e756fd3c97a0d6813c99628ba6d1"
AGENT_ORIGIN = "https://agent.example.test"


class AgentWebConfigTests(unittest.TestCase):
    def test_project_template_has_only_supported_sections(self) -> None:
        config = load_project_config(ROOT / "config.example.json")

        self.assertEqual(
            set(config),
            {"cluster", "server", "agent", "robot"},
        )
        self.assertEqual(config["server"]["host"], "0.0.0.0")

    def test_unknown_agent_key_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.json"
            path.write_text(
                json.dumps(
                    {
                        "cluster": {
                            "control_url": "http://127.0.0.1:8000",
                        },
                        "agent": {"unknown_setting": True},
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "unknown_setting"):
                AgentWebConfig.load(path)

    def test_loads_agent_section_and_shared_control_url(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.json"
            path.write_text(
                json.dumps(
                    {
                        "cluster": {
                            "control_url": "https://control.example.test/",
                        },
                        "agent": {
                            "host": "127.0.0.1",
                            "port": 8200,
                            "auto_open_browser": False,
                        },
                    }
                ),
                encoding="utf-8",
            )

            config = AgentWebConfig.load(path)

            self.assertEqual(config.backend_url, "https://control.example.test")
            self.assertEqual(config.port, 8200)
            self.assertFalse(config.auto_open_browser)

    def test_invalid_control_url_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.json"
            path.write_text(
                json.dumps(
                    {
                        "cluster": {"control_url": "file:///tmp/control"},
                        "agent": {},
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "control_url"):
                AgentWebConfig.load(path)

    def test_tailscale_bind_host_is_resolved(self) -> None:
        result = subprocess.CompletedProcess(
            args=["tailscale", "ip", "-4"],
            returncode=0,
            stdout="100.64.0.2\n",
            stderr="",
        )
        with patch("project_config.subprocess.run", return_value=result):
            self.assertEqual(resolve_bind_host("tailscale"), "100.64.0.2")

    def test_tailscale_bind_host_rejects_invalid_output(self) -> None:
        result = subprocess.CompletedProcess(
            args=["tailscale", "ip", "-4"],
            returncode=0,
            stdout="192.168.1.20\n",
            stderr="",
        )
        with patch("project_config.subprocess.run", return_value=result):
            with self.assertRaisesRegex(ValueError, "100.64.0.0/10"):
                resolve_bind_host("tailscale")


class AgentWebAssetTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        db_path = Path(self.temporary.name) / "agent-web.db"
        self.db = Database(db_path)
        self.db.initialize()
        self.backend_config = ServerConfig(
            app_id="web-test-app",
            app_key="web-test-secret-key",
            db_path=db_path,
            cors_origins=(AGENT_ORIGIN,),
        )
        self.backend_app = create_app(self.backend_config, self.db)

        self.web_server = AgentWebServer(
            AgentWebConfig(
                backend_url="http://127.0.0.1:8000",
                host="127.0.0.1",
                port=0,
                auto_open_browser=False,
            )
        )
        self.web_thread = threading.Thread(
            target=self.web_server.serve_forever,
            daemon=True,
        )
        self.web_thread.start()

    def tearDown(self) -> None:
        self.web_server.server.shutdown()
        self.web_thread.join(timeout=2)
        self.web_server.close()
        self.temporary.cleanup()

    def get_web(self, path: str) -> tuple[int, dict[str, str], bytes]:
        request = Request(f"{self.web_server.url}{path.lstrip('/')}")
        try:
            with urlopen(request, timeout=2) as response:
                return (
                    response.status,
                    {name.lower(): value for name, value in response.headers.items()},
                    response.read(),
                )
        except HTTPError as exc:
            return (
                exc.code,
                {name.lower(): value for name, value in exc.headers.items()},
                exc.read(),
            )

    def test_agent_host_serves_page_and_assets(self) -> None:
        status, headers, body = self.get_web("/")

        self.assertEqual(status, 200)
        self.assertEqual(headers["content-type"], "text/html; charset=utf-8")
        self.assertEqual(headers["x-frame-options"], "DENY")
        text = body.decode("utf-8")
        self.assertIn('href="/styles.css"', text)
        self.assertIn('src="/vendor/aliyun-rtc-sdk.js"', text)
        self.assertIn('src="/app.js"', text)

        for path in (
            "/styles.css",
            "/app.js",
            "/admin.html",
            "/admin.js",
            "/rtc.js",
            "/vendor/aliyun-rtc-sdk.js",
        ):
            with self.subTest(path=path):
                asset_status, _, asset_body = self.get_web(path)
                self.assertEqual(asset_status, 200)
                self.assertGreater(len(asset_body), 100)

        admin_status, _, admin_body = self.get_web("/admin.html")
        self.assertEqual(admin_status, 200)
        self.assertIn("/admin.js", admin_body.decode("utf-8"))

    def test_agent_host_exposes_only_backend_url(self) -> None:
        status, headers, body = self.get_web("/agent-config.json")

        self.assertEqual(status, 200)
        self.assertEqual(headers["cache-control"], "no-store")
        self.assertEqual(
            json.loads(body),
            {"backendUrl": "http://127.0.0.1:8000"},
        )

    def test_control_server_does_not_serve_agent_page(self) -> None:
        status, _, _ = asyncio.run(
            request_bytes(self.backend_app, "GET", "/agent/")
        )

        self.assertEqual(status, 404)

    def test_control_server_allows_configured_agent_origin(self) -> None:
        status, headers, _ = asyncio.run(
            request_bytes(
                self.backend_app,
                "OPTIONS",
                "/api/auth/login",
                headers={
                    "Origin": AGENT_ORIGIN,
                    "Access-Control-Request-Method": "POST",
                    "Access-Control-Request-Headers": "content-type",
                },
            )
        )

        self.assertEqual(status, 200)
        self.assertEqual(headers["access-control-allow-origin"], AGENT_ORIGIN)

    def test_control_server_allows_local_windows_agent_origin(self) -> None:
        origin = "http://127.0.0.1:8100"
        status, headers, _ = asyncio.run(
            request_bytes(
                self.backend_app,
                "OPTIONS",
                "/api/auth/login",
                headers={
                    "Origin": origin,
                    "Access-Control-Request-Method": "POST",
                    "Access-Control-Request-Headers": "content-type",
                },
            )
        )

        self.assertEqual(status, 200)
        self.assertEqual(headers["access-control-allow-origin"], origin)

    def test_local_admin_origin_allows_patch_and_delete(self) -> None:
        origin = "http://127.0.0.1:8100"
        for method in ("PATCH", "DELETE"):
            with self.subTest(method=method):
                status, headers, body = asyncio.run(
                    request_bytes(
                        self.backend_app,
                        "OPTIONS",
                        "/api/admin/robots/example",
                        headers={
                            "Origin": origin,
                            "Access-Control-Request-Method": method,
                            "Access-Control-Request-Headers": (
                                "authorization,content-type"
                                if method == "PATCH"
                                else "authorization"
                            ),
                        },
                    )
                )

                self.assertEqual(status, 200, body)
                self.assertEqual(
                    headers["access-control-allow-origin"],
                    origin,
                )
                self.assertIn(
                    method,
                    {
                        item.strip()
                        for item in headers[
                            "access-control-allow-methods"
                        ].split(",")
                    },
                )

    def test_official_web_sdk_is_pinned_by_hash(self) -> None:
        payload = (AGENT_WEB_DIR / "vendor" / "aliyun-rtc-sdk.js").read_bytes()

        self.assertEqual(hashlib.sha256(payload).hexdigest(), SDK_SHA256)

    def test_agent_files_do_not_contain_app_key(self) -> None:
        for path in AGENT_WEB_DIR.rglob("*"):
            if not path.is_file():
                continue
            with self.subTest(path=path.name):
                self.assertNotIn(
                    self.backend_config.app_key.encode("utf-8"),
                    path.read_bytes(),
                )

    def test_rtc_starts_with_audio_and_video_publish_disabled(self) -> None:
        source = (AGENT_WEB_DIR / "rtc.js").read_text(encoding="utf-8")
        join_index = source.index("await engine.joinChannel")
        audio_disable_index = source.index(
            "await engine.publishLocalAudioStream(false)"
        )
        video_disable_index = source.index(
            "await engine.publishLocalVideoStream(false)"
        )

        self.assertLess(audio_disable_index, join_index)
        self.assertLess(video_disable_index, join_index)

    def test_agent_claims_session_before_requesting_media(self) -> None:
        source = (AGENT_WEB_DIR / "app.js").read_text(encoding="utf-8")
        claim_index = source.index("/claim`")
        token_index = source.index("/viewer-token`")

        self.assertLess(claim_index, token_index)
        self.assertIn("session_released", source)
        self.assertIn("endSessionButton", source)
        self.assertIn("endCurrentSession", source)

    def test_admin_robot_page_has_rename_and_delete_actions(self) -> None:
        source = (AGENT_WEB_DIR / "admin.js").read_text(encoding="utf-8")

        self.assertIn("renameRobot", source)
        self.assertIn("deleteRobot", source)
        self.assertIn('method: "PATCH"', source)
        self.assertIn('method: "DELETE"', source)

    def test_session_cards_use_customer_facing_status_labels(self) -> None:
        source = (AGENT_WEB_DIR / "app.js").read_text(encoding="utf-8")

        for label in (
            "等待客服接入",
            "你正在处理",
            "其他客服处理中",
            "客服通话中",
            "正在离开",
            "正在结束",
            "机器人已离线",
            "麦克风已开",
            "麦克风关闭",
        ):
            self.assertIn(label, source)


class ServerCorsConfigTests(unittest.TestCase):
    def test_server_defaults_to_remote_reachable_listener(self) -> None:
        config = ServerConfig(
            app_id="",
            app_key="",
            db_path=Path("unused.db"),
        )

        self.assertEqual(config.host, "0.0.0.0")

    def test_cors_origins_load_from_config(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.json"
            path.write_text(
                json.dumps(
                    {
                        "server": {
                            "cors_origins": [AGENT_ORIGIN],
                        }
                    }
                ),
                encoding="utf-8",
            )

            config = ServerConfig.load(path)

        self.assertEqual(config.cors_origins, (AGENT_ORIGIN,))

    def test_invalid_cors_origin_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "cors_origins"):
            ServerConfig(
                app_id="",
                app_key="",
                db_path=Path("unused.db"),
                cors_origins=("https://agent.example.test/path",),
            ).validate()


if __name__ == "__main__":
    unittest.main()
